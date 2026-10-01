"""
Public / integration endpoints connecting the CRM store to the B2C website.

Authenticated via X-Integration-Key header (shared secret), not staff login —
same pattern as the registration widget (AllowAny + explicit key check).
"""
from __future__ import annotations

import hmac
import json
import logging
import re
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.models import Branch
from apps.store.inventory_ops import (
    InventoryError,
    adjust_product_stock,
    save_product_inventory,
    serialize_integration_product,
    transfer_product_stock,
)
from apps.store.models import StoreProduct, StoreInvoice
from apps.store.pricing import line_product_amount, order_delivery_amount
from apps.store.stock_utils import available_stock_for_item, peek_size_row
from apps.store.website_fulfillment import (
    parse_delivery_method,
    resolve_pickup_branch,
    website_line_branch,
)
from apps.store.website_integration import (
    link_product_to_website,
    product_in_stock,
    unlink_product_from_website,
    update_product_from_website,
)

logger = logging.getLogger(__name__)

WEBSITE_PAYMENTS_PAUSED_MESSAGE = 'התשלום בכרטיס באתר מושהה זמנית. אפשר לפנות אלינו ונשמח להשלים את ההזמנה.'
WEBSITE_PAYMENT_PAGE_FAILED_MESSAGE = 'התשלום אינו זמין כרגע. נסו שוב בעוד כמה דקות או פנו אלינו.'
WEBSITE_PAYMENT_RECENT_PAGE_MESSAGE = (
    'אנחנו מוודאים שהתשלום הקודם על ההזמנה הזאת לא עבר. אל תשלמו שוב — נסו בעוד כמה דקות, '
    'או פנו אלינו.'
)
WEBSITE_PAYMENT_IN_REVIEW_MESSAGE = (
    'התקבל כבר תשלום על ההזמנה הזאת והוא בבדיקה. אל תשלמו שוב — נעדכן אתכם, '
    'ואפשר גם לפנות אלינו.'
)


def _check_integration_key(request) -> bool:
    expected = getattr(settings, 'WEBSITE_INTEGRATION_API_KEY', '') or ''
    if not expected:
        return False
    provided = (request.headers.get('X-Integration-Key') or '').strip()
    if not provided:
        auth = request.headers.get('Authorization') or ''
        if auth.startswith('Bearer '):
            provided = auth[7:].strip()
    # Constant time: how much of a guess was right must not show in how long
    # the answer took.
    return hmac.compare_digest(provided.encode(), expected.encode())


def _integration_denied():
    return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)


class IntegrationKeyRequired(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = {'error': 'unauthorized'}
    default_code = 'unauthorized'


class _KeyBeforeThrottle:
    """
    For an integration view that is throttled: the key is checked before the
    throttle, so callers without it are turned away without using up the
    shop's rate (every real call comes from the site's one server).
    """

    def initial(self, request, *args, **kwargs):
        if not _check_integration_key(request):
            raise IntegrationKeyRequired()
        return super().initial(request, *args, **kwargs)


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
    """
    Build Tranzila iframe response for a pending website invoice (or short-circuit if already paid).

    Nothing is decided on a copy of the invoice read before a wait: the day
    report, the recheck and the handshake each take seconds, and a notify can
    complete the sale — or report a payment — meanwhile. The invoice is read
    again before each decision, the only write (failed → pending) is a
    conditional UPDATE that re-decides when it finds the row changed, and the
    last look, right before a page is handed out, is whether a payment was
    reported meanwhile (review round 3, 30.9.2026).
    """
    from django.utils import timezone

    from apps.core.payment_service import parse_store_cart_notes
    from apps.core.tranzila_service import TranzilaService
    from apps.store import payment_followup as followup

    def in_review():
        return Response({
            'error': WEBSITE_PAYMENT_IN_REVIEW_MESSAGE,
            'payment_in_review': True,
            'invoice_number': invoice.invoice_number,
        }, status=409)

    def already_paid():
        return Response({
            'ok': True,
            'invoice_number': invoice.invoice_number,
            'invoice_id': str(invoice.id),
            'already_paid': True,
        }, status=status)

    for _attempt in range(3):
        invoice.refresh_from_db()
        if followup.holds_reported_payment(invoice):
            # Tranzila already reported a payment for this order — or the
            # report shows one whose notify never came — that is neither
            # confirmed nor ruled out, whatever the status reads. Asked again
            # first; a second page now could take the same customer's money
            # twice. A suspected charge waits for a person.
            followup.recheck_pending_payment(invoice.pk, site_timeout=followup.POLL_SITE_TIMEOUT_SECONDS)
            invoice.refresh_from_db()
            if invoice.payment_status in followup.MONEY_KEPT_STATUSES:
                return already_paid()
            return in_review()

        if invoice.payment_status in followup.MONEY_KEPT_STATUSES:
            return already_paid()

        if invoice.payment_status == 'refunded':
            # Paid and refunded: a payment now would be taken as the same order
            # paid twice, and sell nothing.
            return Response({'error': 'ההזמנה הזאת זוכתה. צרו הזמנה חדשה.'}, status=400)

        opened = invoice.payment_page_opened_at
        if opened is not None and timezone.now() - opened < followup.SEARCH_WINDOW:
            # A page for this order was handed out before. If its notify never
            # came, the report is the only place the payment shows: a matching
            # charge there is kept on the order ("suspected") and holds it in
            # review until a person decides; a report that cannot say holds
            # it for half an hour from the page (review items 2-4, 30.9.2026).
            found, rows = followup.find_unreported_payment(invoice)
            if found == 'found':
                logger.error('Website order %s: no second page — the day report shows a matching charge',
                             invoice.website_order_number)
                followup.keep_suspected_charges(invoice.pk, rows)
                followup.alert_payment_unreported(invoice, rows)
                return in_review()
            if found == 'unknown' and timezone.now() - opened < followup.UNREPORTED_WINDOW:
                logger.error('Website order %s: no second page — the day report could not be asked in full',
                             invoice.website_order_number)
                followup.alert_payment_unreported(invoice, [])
                return in_review()
            # The report took time: decide again on what the invoice is now.
            invoice.refresh_from_db()
            if followup.holds_reported_payment(invoice) or invoice.payment_status in followup.PAID_STATUSES:
                continue
            if found == 'none' and timezone.now() - opened < followup.REPORT_SETTLE:
                # The report may not list a payment made on the last page
                # minutes ago: "not found" is not "not paid" yet. The customer
                # is asked to wait, not to pay again.
                wait = int((followup.REPORT_SETTLE - (timezone.now() - opened)).total_seconds()) + 1
                logger.warning('Website order %s: no second page yet — the last page opened %ss ago',
                               invoice.website_order_number, int((timezone.now() - opened).total_seconds()))
                return Response({
                    'error': WEBSITE_PAYMENT_RECENT_PAGE_MESSAGE,
                    'payment_in_review': True,
                    'retry_after': wait,
                    'invoice_number': invoice.invoice_number,
                }, status=409)

        if invoice.payment_status == 'failed':
            if parse_store_cart_notes(invoice.notes) is None:
                return Response({'error': 'התשלום הקודם נכשל — צרו הזמנה חדשה'}, status=400)
            reopened = StoreInvoice.objects.filter(
                pk=invoice.pk, payment_status='failed', tranzila_transaction_id='',
            ).update(payment_status='pending')
            if not reopened:
                continue  # changed since it was read: decide again
            invoice.payment_status = 'pending'
        break
    else:
        # Still changing after three reads: nothing is handed out now.
        logger.error('Website order %s: kept changing while a page was asked for', invoice.website_order_number)
        return in_review()

    if not callback_url:
        return Response({'error': 'callback_url required'}, status=400)

    name = (customer.get('name') or invoice.customer_name or '').strip()
    email = (customer.get('email') or '').strip()
    phone = (customer.get('phone') or invoice.customer_phone or '').strip()

    tranzila = TranzilaService.iframe()
    try:
        iframe_url = tranzila.create_payment_request(
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
            offer_wallets=True,
        )
    except Exception as exc:
        # Tranzila's handshake refused or did not answer: no page, so no
        # payment. The order is not left pending — a pending website order
        # counts as income in the reports — and the site may retry it (a
        # failed order that still holds its cart reopens). Only an order no
        # payment was ever reported for is failed: one a notify reported a
        # number for meanwhile (the first page, paid while this retry asked
        # for a second) is in review, and stays so.
        logger.error('Website order %s: Tranzila page not opened: %s', invoice.website_order_number, exc)
        StoreInvoice.objects.filter(
            pk=invoice.pk, payment_status='pending', tranzila_transaction_id='',
        ).update(payment_status='failed')
        followup.alert_payment_page_failed(invoice, str(exc), tranzila.terminal)
        return Response({'error': WEBSITE_PAYMENT_PAGE_FAILED_MESSAGE}, status=503)

    # The last look before a page leaves: a payment reported, or a sale
    # completed, while the page was being asked for.
    invoice.refresh_from_db()
    if invoice.payment_status in followup.MONEY_KEPT_STATUSES:
        return already_paid()
    if followup.holds_reported_payment(invoice) or invoice.payment_status == 'refunded':
        return in_review()
    now = timezone.now()
    StoreInvoice.objects.filter(pk=invoice.pk).update(payment_page_opened_at=now)
    StoreInvoice.objects.filter(pk=invoice.pk, payment_page_first_opened_at__isnull=True).update(
        payment_page_first_opened_at=now,
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

        # Paused before anything is written or any payment page is opened —
        # for a new order and for one the site retries. See both settings: the
        # page this view opens is Tranzila's hosted page, which is switched off
        # while it runs on the test terminal.
        if not (settings.STORE_WEBSITE_CARD_PAYMENTS_ENABLED and settings.TRANZILA_HOSTED_PAGE_ENABLED):
            # The website moves the whole page to whatever payment address it
            # gets back, so while payment is off it gets ours: a kind
            # "temporarily closed" page with a way to reach the office, in
            # place of an error. Still nothing written, nothing charged.
            from apps.core.password_reset_email import crm_frontend_url
            return Response({
                'ok': True,
                'iframe_url': f'{crm_frontend_url()}/store-closed',
                'payments_paused': True,
            })

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
        address = (customer.get('address') or customer.get('shipping_address') or '').strip()[:255]
        customer_notes = (customer.get('notes') or '').strip()

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
                    shipping_address=address,
                    customer_notes=customer_notes,
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


class WidgetStorePaymentStatusView(_KeyBeforeThrottle, APIView):
    """
    GET /api/v1/store/widget/payment/status/?order=<website_order_number>
    → {"status": "pending" | "completed" | "failed" | "refunded",
       "invoice_number": "ST-…", "paid": bool, "payment_reported": bool}

      paid              true only for a Tranzila payment the report confirmed.
      payment_reported  true while Tranzila reported a payment the CRM has
                        neither confirmed nor ruled out: the order is in
                        review, status is "pending", and the customer must
                        not be asked to pay again.
      refunded          paid and then refunded: not paid.
    (payment_followup.site_status is the one place this is decided.)

    The website's poll while the customer waits on the result page. The
    answer is always the CRM's own record. On the way, a payment Tranzila
    reported and the report has not confirmed yet is asked about again (at
    most once per 15 seconds per order, however often the site polls), and a
    paid order the site never acknowledged is told again — both through
    apps/store/payment_followup.py, neither charges anything. Authenticated
    with the integration key, like the rest of the widget, checked before the
    throttle. Throttled: every poll comes from the site's own server, so the
    rate is for the whole shop.
    """
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'store_payment_status'

    def get(self, request):
        # The key was checked before the throttle (_KeyBeforeThrottle.initial).
        order = (request.query_params.get('order') or '').strip()
        if not order:
            return Response({'error': 'order required'}, status=400)
        from apps.store.payment_followup import website_order_status

        payload = website_order_status(order)
        if payload is None:
            return Response({'error': 'order not found'}, status=404)
        return Response(payload)


class WidgetStorePaymentReturnedView(_KeyBeforeThrottle, APIView):
    """
    POST /api/v1/store/widget/payment/returned/
    {"order": "<website_order_number>", "index": "<Tranzila's transaction number>", "code": "<ConfirmationCode>"}
    → the same answer as the status endpoint.

    Server to server, from the website, with the integration key: the number
    Tranzila's page handed back to the site's return address, for an order
    whose notify may never come. It is recorded as reported and judged by the
    report exactly like a notify — believed no more than one. `code` (the
    approval number the page returned) is optional; without it the report
    cannot tie the number to the order, and the order stays in review for a
    person. A number that is not this order's payment does not pass, and is
    kept for the office like any other. Call it only for the page's
    success return.

    Refused (409, with the status) for an order whose page the CRM did not
    hand out in the last two hours; 400 for anything but ASCII digits. At
    most three undecided numbers are kept per order.
    """
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'store_payment_returned'

    def post(self, request):
        # The key was checked before the throttle (_KeyBeforeThrottle.initial).
        from apps.store import payment_followup

        order = str(request.data.get('order') or '').strip()
        index = str(request.data.get('index') or '').strip()
        code = str(request.data.get('code') or request.data.get('ConfirmationCode') or '').strip()[:100]
        if not order:
            return Response({'error': 'order required'}, status=400)
        if not payment_followup.is_transaction_number(index):
            return Response({'error': 'index must be the transaction number Tranzila returned'}, status=400)
        outcome, payload = payment_followup.record_returned_number(order, index, code)
        if outcome == payment_followup.RETURNED_NO_ORDER:
            return Response({'error': 'order not found'}, status=404)
        if outcome == payment_followup.RETURNED_NO_RECENT_PAGE:
            return Response({**payload, 'error': 'no payment page was opened for this order in the last two hours'},
                            status=409)
        return Response(payload)


class WidgetStoreWebsiteOrderView(APIView):
    """
    Retired (29.9.2026). POST /api/v1/store/widget/order/ answers 410 and changes nothing.

    It opened a *completed* invoice for a website order — the sale recorded,
    the stock taken, the document signed and mailed — with no payment behind
    it at all: a caller holding the integration key could "buy" anything.
    The website pays through widget/payment/initiate/ (Tranzila's page,
    confirmed against the terminal's report). Before closing it, nothing in
    kogo-front or cogomelo-site called it, on any branch — the site keeps an
    unused client function (submitCrmWebsiteOrder) and its contract document.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        logger.warning(
            'Retired website order endpoint called (integration key valid: %s); nothing written',
            _check_integration_key(request),
        )
        return Response(
            {'error': 'endpoint closed', 'use': '/api/v1/store/widget/payment/initiate/'},
            status=status.HTTP_410_GONE,
        )
