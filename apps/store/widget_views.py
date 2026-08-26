"""
Public / integration endpoints connecting the CRM store to the B2C website.

Authenticated via X-Integration-Key header (shared secret), not staff login —
same pattern as the registration widget (AllowAny + explicit key check).
"""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import Branch
from apps.store.inventory_ops import (
    InventoryError,
    adjust_product_stock,
    save_product_inventory,
    serialize_integration_product,
    transfer_product_stock,
)
from apps.store.models import StoreProduct, StoreInvoice, StoreSale
from apps.store.pricing import line_product_amount, order_delivery_amount, sale_unit_and_total
from apps.store.stock_utils import (
    available_stock_for_item,
    decrement_product_stock,
    peek_size_row,
    store_line_item_branch_id,
)
from apps.store.website_fulfillment import (
    parse_delivery_method,
    resolve_pickup_branch,
    website_line_branch,
)
from apps.store.website_integration import (
    link_product_to_website,
    product_in_stock,
    push_product_to_website,
    unlink_product_from_website,
    update_product_from_website,
)

logger = logging.getLogger(__name__)


def _check_integration_key(request) -> bool:
    expected = getattr(settings, 'WEBSITE_INTEGRATION_API_KEY', '') or ''
    if not expected:
        return False
    provided = (request.headers.get('X-Integration-Key') or '').strip()
    if not provided:
        auth = request.headers.get('Authorization') or ''
        if auth.startswith('Bearer '):
            provided = auth[7:].strip()
    return provided == expected


def _integration_denied():
    return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)


def _serialize_integration_product(p: StoreProduct) -> dict:
    return serialize_integration_product(p)


def _get_product(product_id: str) -> StoreProduct:
    """Inventory admin must reach linked products even if is_active is False."""
    try:
        return (
            StoreProduct.objects
            .select_related('branch')
            .prefetch_related('size_stocks__branch')
            .get(pk=product_id)
        )
    except (StoreProduct.DoesNotExist, ValidationError, ValueError):
        raise InventoryError('product not found', status=404)


class IntegrationProductsView(APIView):
    """
    GET /api/v1/store/integration/products/
    List active CRM products for linking in the B2C admin.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        if not _check_integration_key(request):
            return _integration_denied()
        qs = (
            StoreProduct.objects.filter(Q(is_active=True) | Q(website_legacy_id__isnull=False))
            .select_related('branch')
            .prefetch_related('size_stocks__branch')
            .distinct()
            .order_by('name')
        )
        branch = request.query_params.get('branch')
        if branch == 'delivery':
            qs = qs.filter(branch__isnull=True)
        elif branch and branch != 'all':
            qs = qs.filter(branch_id=branch)
        return Response([_serialize_integration_product(p) for p in qs])


class IntegrationLinkView(APIView):
    """
    POST /api/v1/store/integration/link/
    Body: { "crm_product_id": "<uuid>", "website_legacy_id": 12345 }

    An explicit `"website_legacy_id": null` unlinks the product. The key must
    still be present — treating "absent" as "unlink" would turn a malformed
    request into silent data loss.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not _check_integration_key(request):
            return _integration_denied()
        crm_product_id = (request.data.get('crm_product_id') or '').strip()
        if not crm_product_id:
            return Response({'error': 'crm_product_id is required'}, status=400)
        if 'website_legacy_id' not in request.data:
            return Response({'error': 'website_legacy_id is required (null to unlink)'}, status=400)

        website_legacy_id = request.data.get('website_legacy_id')
        try:
            if website_legacy_id is None:
                product = unlink_product_from_website(product_id=crm_product_id)
            else:
                product = link_product_to_website(
                    product_id=crm_product_id,
                    website_legacy_id=int(website_legacy_id),
                )
        except (TypeError, ValueError):
            return Response({'error': 'website_legacy_id must be an integer or null'}, status=400)
        except StoreProduct.DoesNotExist:
            return Response({'error': 'product not found'}, status=404)
        except ValidationError:
            return Response({'error': 'crm_product_id is not a valid id'}, status=400)
        return Response({'ok': True, 'product': _serialize_integration_product(product)})


class IntegrationProductUpdateView(APIView):
    """
    POST /api/v1/store/integration/update/
    Body: {
      "website_legacy_id": 12570,
      "sale_price": 169,
      "branch_only": false,
      "in_stock": true
    }
    B2C admin pushes price/stock flags here; CRM post_save mirrors them to the site.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not _check_integration_key(request):
            return _integration_denied()

        website_legacy_id = request.data.get('website_legacy_id')
        crm_product_id = (request.data.get('crm_product_id') or '').strip() or None
        if website_legacy_id is None and not crm_product_id:
            return Response({'error': 'website_legacy_id or crm_product_id is required'}, status=400)

        sale_price = request.data.get('sale_price')
        branch_only = request.data.get('branch_only')
        in_stock = request.data.get('in_stock')
        image_url = request.data.get('image_url')

        if sale_price is None and branch_only is None and in_stock is None and not image_url:
            return Response({'error': 'nothing to update'}, status=400)

        try:
            if website_legacy_id is not None:
                website_legacy_id = int(website_legacy_id)
        except (TypeError, ValueError):
            return Response({'error': 'website_legacy_id must be an integer'}, status=400)

        try:
            if sale_price is not None:
                sale_price = Decimal(str(sale_price))
        except (TypeError, ValueError):
            return Response({'error': 'sale_price must be a number'}, status=400)

        try:
            product = update_product_from_website(
                website_legacy_id=website_legacy_id,
                crm_product_id=crm_product_id,
                sale_price=sale_price,
                branch_only=branch_only if branch_only is not None else None,
                in_stock=in_stock if in_stock is not None else None,
                image_url=(image_url or '').strip() or None,
            )
        except StoreProduct.DoesNotExist:
            return Response({'error': 'product not found'}, status=404)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        return Response({'ok': True, 'product': _serialize_integration_product(product)})


class IntegrationBranchesView(APIView):
    """
    GET /api/v1/store/integration/branches/
    Active CRM branches for the B2C inventory location picker.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        if not _check_integration_key(request):
            return _integration_denied()
        qs = Branch.objects.filter(is_active=True).order_by('name')
        return Response([{'id': str(b.id), 'name': b.name} for b in qs])


class IntegrationProductInventoryView(APIView):
    """
    GET   /api/v1/store/integration/products/<uuid>/inventory/
    PATCH /api/v1/store/integration/products/<uuid>/inventory/

    Read / replace size-location stock rows and the min-stock alert — same
    fields the CRM product editor saves, without exposing sale price.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, product_id=None):
        if not _check_integration_key(request):
            return _integration_denied()
        try:
            product = _get_product(product_id)
        except InventoryError as exc:
            return Response({'error': exc.message}, status=exc.status)
        return Response(_serialize_integration_product(product))

    def patch(self, request, product_id=None):
        if not _check_integration_key(request):
            return _integration_denied()
        try:
            product = _get_product(product_id)
            product = save_product_inventory(product, request.data if isinstance(request.data, dict) else {})
        except InventoryError as exc:
            return Response({'error': exc.message}, status=exc.status)
        return Response({'ok': True, 'product': _serialize_integration_product(product)})


class IntegrationAdjustStockView(APIView):
    """
    POST /api/v1/store/integration/products/<uuid>/adjust_stock/
    Same audited adjust as the CRM staff action (receipt / theft / damage / recount / other).
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, product_id=None):
        if not _check_integration_key(request):
            return _integration_denied()
        try:
            quantity_delta = int(request.data.get('quantity_delta', 0))
        except (TypeError, ValueError):
            return Response({'error': 'quantity_delta must be an integer'}, status=400)
        try:
            product = _get_product(product_id)
            product = adjust_product_stock(
                product,
                quantity_delta=quantity_delta,
                reason=request.data.get('reason', ''),
                note=request.data.get('note', '') or '',
                size_stock_id=(request.data.get('size_stock_id') or '').strip() or None,
            )
        except InventoryError as exc:
            return Response({'error': exc.message}, status=exc.status)
        return Response({'ok': True, 'product': _serialize_integration_product(product)})


class IntegrationTransferStockView(APIView):
    """
    POST /api/v1/store/integration/products/<uuid>/transfer_stock/
    Move units between two size/location rows of the same product.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, product_id=None):
        if not _check_integration_key(request):
            return _integration_denied()
        try:
            quantity = int(request.data.get('quantity', 0))
        except (TypeError, ValueError):
            return Response({'error': 'quantity must be a positive integer'}, status=400)
        try:
            product = _get_product(product_id)
            product = transfer_product_stock(
                product,
                quantity=quantity,
                from_size_stock_id=request.data.get('from_size_stock_id') or '',
                to_size_stock_id=request.data.get('to_size_stock_id') or '',
            )
        except InventoryError as exc:
            return Response({'error': exc.message}, status=exc.status)
        return Response({'ok': True, 'product': _serialize_integration_product(product)})


class WidgetStoreStockCheckView(APIView):
    """
    POST /api/v1/store/widget/stock-check/
    Body: {
      "items": [{ "legacy_id": 123, "quantity": 2, "variant": "M" }],
      "delivery_method": "delivery" | "pickup",
      "pickup_branch_id": "<uuid>"  // optional; resolved to אם המושבות when omitted
    }
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not _check_integration_key(request):
            return _integration_denied()
        items = request.data.get('items') or []
        if not isinstance(items, list) or not items:
            return Response({'error': 'items required'}, status=400)

        try:
            delivery_method = parse_delivery_method(request.data.get('delivery_method'))
            pickup_branch = (
                resolve_pickup_branch(request.data.get('pickup_branch_id'))
                if delivery_method == 'pickup'
                else None
            )
            branch = website_line_branch(
                delivery_method=delivery_method,
                pickup_branch=pickup_branch,
            )
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        results = []
        all_ok = True
        for raw in items:
            legacy_id = raw.get('legacy_id')
            qty = int(raw.get('quantity') or 0)
            variant = (raw.get('variant') or raw.get('size') or '').strip()
            try:
                legacy_id = int(legacy_id)
            except (TypeError, ValueError):
                results.append({'legacy_id': legacy_id, 'ok': False, 'error': 'invalid legacy_id'})
                all_ok = False
                continue
            product = (
                StoreProduct.objects.prefetch_related('size_stocks')
                .filter(website_legacy_id=legacy_id, is_active=True)
                .first()
            )
            if not product:
                results.append({'legacy_id': legacy_id, 'ok': False, 'error': 'not linked to CRM'})
                all_ok = False
                continue
            if qty <= 0:
                results.append({'legacy_id': legacy_id, 'ok': False, 'error': 'invalid quantity'})
                all_ok = False
                continue

            stock_item = {'quantity': qty, 'size': variant, 'branch': branch}
            available = available_stock_for_item(product, stock_item)
            ok = available >= qty

            if not ok:
                all_ok = False
            results.append({
                'legacy_id': legacy_id,
                'ok': ok,
                'in_stock': product_in_stock(product),
                'available': available,
                'sale_price': str(product.sale_price),
                'name': product.name,
            })

        return Response({'ok': True, 'items': results})


def _fulfillment_from_request(data):
    delivery_method = parse_delivery_method(data.get('delivery_method'))
    pickup_branch = None
    if delivery_method == 'pickup':
        pickup_branch = resolve_pickup_branch(data.get('pickup_branch_id'))
    branch = website_line_branch(delivery_method=delivery_method, pickup_branch=pickup_branch)
    return delivery_method, pickup_branch, branch


def _resolve_website_cart_items(items, *, delivery_method='delivery', pickup_branch=None):
    """
    Resolve B2C legacy_id lines to CRM products under row lock.
    Returns (total, resolved_lines, webhook_product_items).

    Shipping is once per order on delivery. Pickup uses the branch location
    (size_stock_id) and adds no delivery fee.
    """
    branch = website_line_branch(delivery_method=delivery_method, pickup_branch=pickup_branch)
    is_delivery = delivery_method != 'pickup'
    total = Decimal('0.00')
    resolved = []
    product_items = []
    products = []
    for raw in items:
        legacy_id = int(raw['legacy_id'])
        qty = int(raw.get('quantity') or raw.get('qty') or 0)
        variant = (raw.get('variant') or raw.get('size') or '').strip()
        if qty <= 0:
            raise ValueError('כמות לא תקינה')
        product = (
            StoreProduct.objects.select_for_update()
            .prefetch_related('size_stocks')
            .filter(website_legacy_id=legacy_id, is_active=True)
            .first()
        )
        if not product:
            raise ValueError(f'מוצר {legacy_id} לא מקושר ל-CRM')
        stock_item = {'quantity': qty, 'size': variant, 'branch': branch}
        if available_stock_for_item(product, stock_item) < qty:
            raise ValueError(f'אין מספיק מלאי עבור {product.name}')
        size_row = peek_size_row(product, stock_item)
        line_total = line_product_amount(product, qty)
        total += line_total
        products.append(product)
        resolved.append({
            'product': product,
            'quantity': qty,
            'size': variant,
            'unit_price': product.sale_price,
            'line_total': line_total,
        })
        product_items.append({
            'product_id': str(product.id),
            'quantity': qty,
            'size': variant,
            'branch': branch,
            'size_stock_id': str(size_row.id) if size_row else None,
            'line_delivery': False,
        })
    total += order_delivery_amount(products, is_delivery=is_delivery)
    return total, resolved, product_items


def _website_payment_initiate_response(invoice, *, callback_url, success_url, error_url, customer, status=200):
    """Build Tranzila iframe response for a pending website invoice (or short-circuit if already paid)."""
    from apps.core.payment_service import parse_store_cart_notes
    from apps.core.tranzila_service import TranzilaService

    if invoice.payment_status == 'completed':
        return Response({
            'ok': True,
            'invoice_number': invoice.invoice_number,
            'invoice_id': str(invoice.id),
            'already_paid': True,
        }, status=status)

    if invoice.payment_status == 'failed':
        if parse_store_cart_notes(invoice.notes) is None:
            return Response({'error': 'התשלום הקודם נכשל — צרו הזמנה חדשה'}, status=400)
        invoice.payment_status = 'pending'
        invoice.save(update_fields=['payment_status'])

    if not callback_url:
        return Response({'error': 'callback_url required'}, status=400)

    name = (customer.get('name') or invoice.customer_name or '').strip()
    email = (customer.get('email') or '').strip()
    phone = (customer.get('phone') or invoice.customer_phone or '').strip()

    iframe_url = TranzilaService.iframe().create_payment_request(
        amount=invoice.total_amount,
        currency='ILS',
        description=f"Website order {invoice.website_order_number or invoice.invoice_number}",
        customer_name=name,
        customer_email=email,
        customer_phone=re.sub(r'\D', '', phone)[:15],
        success_url=success_url,
        error_url=error_url,
        callback_url=callback_url,
        transaction_id=str(invoice.id),
    )

    return Response({
        'ok': True,
        'iframe_url': iframe_url,
        'invoice_number': invoice.invoice_number,
        'invoice_id': str(invoice.id),
    }, status=status)


class WidgetStorePaymentInitiateView(APIView):
    """
    POST /api/v1/store/widget/payment/initiate/
    Pending CRM invoice + Tranzila payment URL for a B2C website checkout.
    Stock is decremented only after the Tranzila webhook confirms payment.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not _check_integration_key(request):
            return _integration_denied()

        idempotency_key = (request.data.get('idempotency_key') or '').strip() or None
        website_order_number = (request.data.get('website_order_number') or '').strip() or None
        customer = request.data.get('customer') or {}
        items = request.data.get('items') or []
        callback_url = (request.data.get('callback_url') or '').strip()
        success_url = (request.data.get('success_url') or '').strip()
        error_url = (request.data.get('error_url') or '').strip()

        if not items:
            return Response({'error': 'items required'}, status=400)
        if not website_order_number:
            return Response({'error': 'website_order_number required'}, status=400)

        if idempotency_key:
            existing = StoreInvoice.objects.filter(website_idempotency_key=idempotency_key).first()
            if existing:
                return _website_payment_initiate_response(
                    existing,
                    callback_url=callback_url,
                    success_url=success_url,
                    error_url=error_url,
                    customer=customer,
                )

        if website_order_number:
            existing = StoreInvoice.objects.filter(website_order_number=website_order_number).first()
            if existing:
                return _website_payment_initiate_response(
                    existing,
                    callback_url=callback_url,
                    success_url=success_url,
                    error_url=error_url,
                    customer=customer,
                )

        name = (customer.get('name') or '').strip()
        phone = (customer.get('phone') or '').strip()
        email = (customer.get('email') or '').strip()

        try:
            delivery_method, pickup_branch, _branch = _fulfillment_from_request(request.data)
            with transaction.atomic():
                total, resolved, product_items = _resolve_website_cart_items(
                    items,
                    delivery_method=delivery_method,
                    pickup_branch=pickup_branch,
                )

                if total < Decimal('1.00'):
                    raise ValueError('סכום מינימלי לתשלום מקוון: ₪1')

                invoice = StoreInvoice(
                    customer_name=name,
                    customer_phone=phone,
                    customer_email=email,
                    total_amount=total,
                    payment_method='credit_card',
                    payment_status='pending',
                    charged_with_token=False,
                    website_order_number=website_order_number,
                    website_idempotency_key=idempotency_key,
                    notes=json.dumps(product_items),
                    branch=pickup_branch if pickup_branch else (
                        resolved[0]['product'].branch if resolved else None
                    ),
                )
                invoice.save()

        except (KeyError, TypeError, ValueError) as exc:
            return Response({'error': str(exc)}, status=400)
        except Exception:
            logger.exception('Website payment initiate failed')
            return Response({'error': 'שגיאה בפתיחת התשלום'}, status=500)

        return _website_payment_initiate_response(
            invoice,
            callback_url=callback_url,
            success_url=success_url,
            error_url=error_url,
            customer=customer,
            status=201,
        )


class WidgetStoreWebsiteOrderView(APIView):
    """
    POST /api/v1/store/widget/order/
    Creates a CRM store invoice from a B2C website order (walk-in customer).
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not _check_integration_key(request):
            return _integration_denied()

        idempotency_key = (request.data.get('idempotency_key') or '').strip() or None
        website_order_number = (request.data.get('website_order_number') or '').strip() or None
        customer = request.data.get('customer') or {}
        items = request.data.get('items') or []

        if idempotency_key:
            existing = StoreInvoice.objects.filter(website_idempotency_key=idempotency_key).first()
            if existing:
                return Response({
                    'ok': True,
                    'invoice_number': existing.invoice_number,
                    'invoice_id': str(existing.id),
                    'duplicate': True,
                })

        if website_order_number:
            existing = StoreInvoice.objects.filter(website_order_number=website_order_number).first()
            if existing:
                return Response({
                    'ok': True,
                    'invoice_number': existing.invoice_number,
                    'invoice_id': str(existing.id),
                    'duplicate': True,
                })

        if not items:
            return Response({'error': 'items required'}, status=400)

        name = (customer.get('name') or '').strip()
        phone = (customer.get('phone') or '').strip()
        email = (customer.get('email') or '').strip()

        try:
            delivery_method, pickup_branch, _branch = _fulfillment_from_request(request.data)
            with transaction.atomic():
                total, resolved, product_items = _resolve_website_cart_items(
                    items,
                    delivery_method=delivery_method,
                    pickup_branch=pickup_branch,
                )

                notes_parts = ['הזמנה מהאתר']
                if delivery_method == 'pickup':
                    notes_parts.append('איסוף מהסניף')
                if email:
                    notes_parts.append(f'email:{email}')
                if website_order_number:
                    notes_parts.append(f'web:{website_order_number}')

                invoice = StoreInvoice(
                    customer_name=name,
                    customer_phone=phone,
                    customer_email=email,
                    total_amount=total,
                    payment_method='credit_card',
                    # B2C checkout confirms the sale; stock is decremented immediately.
                    payment_status='completed',
                    charged_with_token=False,
                    website_order_number=website_order_number,
                    website_idempotency_key=idempotency_key,
                    notes=' | '.join(notes_parts),
                    branch=pickup_branch if pickup_branch else (
                        resolved[0]['product'].branch if resolved else None
                    ),
                )
                invoice.save()

                for item in product_items:
                    product = StoreProduct.objects.get(id=item['product_id'])
                    unit, line_total = sale_unit_and_total(product, item)
                    StoreSale.objects.create(
                        invoice=invoice,
                        product=product,
                        child=None,
                        quantity=item['quantity'],
                        unit_price=unit,
                        total_price=line_total,
                        size=item.get('size', ''),
                        payment_method='credit_card',
                        branch_id=store_line_item_branch_id(item, product),
                        notes='website order',
                    )
                    decrement_product_stock(product, item)
                    product.refresh_from_db(fields=['stock_quantity'])
                    push_product_to_website(product)

        except (KeyError, TypeError, ValueError) as exc:
            return Response({'error': str(exc)}, status=400)
        except Exception as exc:
            logger.exception('Website order failed')
            return Response({'error': 'שגיאה ביצירת ההזמנה'}, status=500)

        try:
            from apps.store.invoice_email import send_store_invoice_email
            send_store_invoice_email(invoice)
        except Exception:
            logger.exception('Store invoice email failed for %s (non-fatal)', invoice.invoice_number)

        return Response({
            'ok': True,
            'invoice_number': invoice.invoice_number,
            'invoice_id': str(invoice.id),
        }, status=201)
