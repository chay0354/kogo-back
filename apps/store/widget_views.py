"""
Public / integration endpoints connecting the CRM store to the B2C website.

Authenticated via X-Integration-Key header (shared secret), not staff login —
same pattern as the registration widget (AllowAny + explicit key check).
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.store.models import StoreProduct, StoreInvoice, StoreSale
from apps.store.stock_utils import decrement_product_stock, store_line_item_branch_id
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
    sizes = []
    for row in p.size_stocks.all():
        sizes.append({
            'size_stock_id': str(row.id),
            'size': row.size,
            'in_stock': row.stock_quantity > 0,
        })
    return {
        'id': str(p.id),
        'name': p.name,
        'category': p.category,
        'sale_price': str(p.sale_price),
        'image_url': p.image_url or '',
        'stock_quantity': int(p.stock_quantity or 0),
        'in_stock': product_in_stock(p),
        'website_legacy_id': p.website_legacy_id,
        'branch_only': p.branch_only,
        'branch': str(p.branch_id) if p.branch_id else None,
        'sizes': sizes,
    }


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
            StoreProduct.objects.filter(is_active=True)
            .prefetch_related('size_stocks')
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


class WidgetStoreStockCheckView(APIView):
    """
    POST /api/v1/store/widget/stock-check/
    Body: { "items": [{ "legacy_id": 123, "quantity": 2, "variant": "M" }] }
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not _check_integration_key(request):
            return _integration_denied()
        items = request.data.get('items') or []
        if not isinstance(items, list) or not items:
            return Response({'error': 'items required'}, status=400)

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

            available = int(product.stock_quantity or 0)
            ok = available >= qty
            if variant and product.has_per_size_stock():
                row = product.size_stocks.filter(size=variant).order_by('sort_order').first()
                if row:
                    available = int(row.stock_quantity or 0)
                    ok = available >= qty
                else:
                    ok = False

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


def _resolve_website_cart_items(items):
    """
    Resolve B2C legacy_id lines to CRM products under row lock.
    Returns (total, resolved_lines, webhook_product_items).
    """
    total = Decimal('0.00')
    resolved = []
    product_items = []
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
        if int(product.stock_quantity or 0) < qty:
            raise ValueError(f'אין מספיק מלאי עבור {product.name}')
        line_total = product.sale_price * qty
        total += line_total
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
            'branch': 'delivery',
        })
    return total, resolved, product_items


def _website_payment_initiate_response(invoice, *, callback_url, success_url, error_url, customer, status=200):
    """Build Tranzila iframe response for a pending website invoice (or short-circuit if already paid)."""
    from apps.core.payment_service import PaymentService

    if invoice.payment_status == 'completed':
        return Response({
            'ok': True,
            'invoice_number': invoice.invoice_number,
            'invoice_id': str(invoice.id),
            'already_paid': True,
        }, status=status)

    if invoice.payment_status == 'failed':
        return Response({'error': 'התשלום הקודם נכשל — צרו הזמנה חדשה'}, status=400)

    if not callback_url:
        return Response({'error': 'callback_url required'}, status=400)

    payment_service = PaymentService()
    name = (customer.get('name') or invoice.customer_name or '').strip()
    email = (customer.get('email') or '').strip()
    phone = (customer.get('phone') or invoice.customer_phone or '').strip()

    iframe_url = payment_service.tranzila_service.create_payment_request(
        amount=invoice.total_amount,
        currency='ILS',
        description=f"הזמנה מהאתר {invoice.website_order_number or invoice.invoice_number}",
        customer_name=name,
        customer_email=email,
        customer_phone=phone,
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
            with transaction.atomic():
                total, resolved, product_items = _resolve_website_cart_items(items)

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
                    branch=resolved[0]['product'].branch if resolved else None,
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
            with transaction.atomic():
                total, resolved, _product_items = _resolve_website_cart_items(items)

                notes_parts = ['הזמנה מהאתר']
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
                    branch=resolved[0]['product'].branch if resolved else None,
                )
                invoice.save()

                for line in resolved:
                    product = line['product']
                    item = {'quantity': line['quantity'], 'size': line['size'], 'branch': 'delivery'}
                    if int(product.stock_quantity or 0) < line['quantity']:
                        raise ValueError(f'אין מספיק מלאי עבור {product.name}')

                    StoreSale.objects.create(
                        invoice=invoice,
                        product=product,
                        child=None,
                        quantity=line['quantity'],
                        unit_price=line['unit_price'],
                        total_price=line['line_total'],
                        size=line['size'],
                        payment_method='credit_card',
                        branch_id=store_line_item_branch_id(item, product),
                        notes='website order',
                    )
                    decrement_product_stock(product, {
                        'product_id': str(product.id),
                        'quantity': line['quantity'],
                        'size': line['size'],
                        'branch': 'delivery',
                    })
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
