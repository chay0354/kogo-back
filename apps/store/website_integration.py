"""
Push store stock/price updates to the B2C public website.

The website exposes POST /api/integrations/stock (Bearer INTEGRATION_API_KEY).
CRM calls it whenever a linked product's stock or sale price changes.
"""
from __future__ import annotations

import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.db import transaction

from apps.store.models import StoreProduct

logger = logging.getLogger(__name__)


def _integration_configured() -> bool:
    return bool(getattr(settings, 'WEBSITE_INTEGRATION_URL', '') and getattr(settings, 'WEBSITE_INTEGRATION_API_KEY', ''))


def product_in_stock(product: StoreProduct) -> bool:
    return int(product.stock_quantity or 0) > 0


def normalize_website_image_url(path: str) -> str:
    """Turn B2C relative image paths into absolute URLs for CRM storage."""
    path = (path or '').strip()
    if not path:
        return ''
    if path.startswith(('http://', 'https://')):
        return path
    base = getattr(settings, 'WEBSITE_INTEGRATION_URL', '').rstrip('/')
    if not base:
        return path
    return f'{base}{path}' if path.startswith('/') else f'{base}/{path}'


def push_product_to_website(product: StoreProduct) -> bool:
    return push_products_batch_to_website([product]) > 0


def push_products_batch_to_website(products: list[StoreProduct]) -> int:
    """Push stock/price for many linked products in one (or few) website API calls."""
    if not _integration_configured() or not products:
        return 0

    items: list[dict] = []
    seen: set[int] = set()
    for product in products:
        legacy_id = product.website_legacy_id
        if not legacy_id or legacy_id in seen:
            continue
        seen.add(int(legacy_id))
        items.append({
            'legacy_id': int(legacy_id),
            'in_stock': product_in_stock(product),
            'price': float(product.sale_price or Decimal('0')),
            'purchasable': not product.branch_only,
        })
    if not items:
        return 0

    url = settings.WEBSITE_INTEGRATION_URL.rstrip('/') + '/api/integrations/stock'
    headers = {
        'Authorization': f'Bearer {settings.WEBSITE_INTEGRATION_API_KEY}',
        'Content-Type': 'application/json',
    }
    pushed = 0
    chunk_size = 100
    for i in range(0, len(items), chunk_size):
        chunk = items[i:i + chunk_size]
        try:
            resp = requests.post(url, json={'items': chunk}, headers=headers, timeout=30)
            if resp.status_code >= 400:
                logger.error(
                    'Website batch push failed: HTTP %s legacy_ids=%s body=%s',
                    resp.status_code,
                    [i.get('legacy_id') for i in chunk],
                    resp.text[:500],
                )
                continue
            body = resp.json() if resp.content else {}
            pushed += int(body.get('updated', len(chunk)))
        except requests.RequestException as exc:
            logger.warning('Website batch push error: %s', exc)
    if pushed:
        logger.info('Batch-pushed %s product(s) to website', pushed)
    return pushed


def link_product_to_website(*, product_id: str, website_legacy_id: int) -> StoreProduct:
    """Set website_legacy_id on a CRM product (used by integration API)."""
    legacy_id = int(website_legacy_id)
    with transaction.atomic():
        product = StoreProduct.objects.get(pk=product_id, is_active=True)
        # website_legacy_id is unique. Re-pointing a website product at a
        # different CRM product would otherwise hit IntegrityError, so release
        # the id from whoever holds it first.
        StoreProduct.objects.filter(website_legacy_id=legacy_id).exclude(pk=product.pk).update(
            website_legacy_id=None
        )
        product.website_legacy_id = legacy_id
        product.save(update_fields=['website_legacy_id', 'updated_at'])
    # Outside the transaction: an outbound HTTP call must not hold row locks.
    push_product_to_website(product)
    return product


def unlink_product_from_website(*, product_id: str) -> StoreProduct:
    """Clear website_legacy_id so the CRM product is no longer synced to the site."""
    product = StoreProduct.objects.get(pk=product_id)
    if product.website_legacy_id is not None:
        product.website_legacy_id = None
        product.save(update_fields=['website_legacy_id', 'updated_at'])
    return product


def update_product_from_website(
    *,
    website_legacy_id: int | None = None,
    crm_product_id: str | None = None,
    sale_price: Decimal | float | None = None,
    branch_only: bool | None = None,
    in_stock: bool | None = None,
    image_url: str | None = None,
) -> StoreProduct:
    """
    Apply price/stock flags pushed from the B2C admin for a linked product.
    post_save pushes the same values back to the website so both sides stay aligned.
    """
    if website_legacy_id is not None:
        product = StoreProduct.objects.get(website_legacy_id=int(website_legacy_id), is_active=True)
    elif crm_product_id:
        product = StoreProduct.objects.get(pk=crm_product_id, is_active=True)
    else:
        raise ValueError('website_legacy_id or crm_product_id is required')

    update_fields: list[str] = []
    if sale_price is not None:
        product.sale_price = Decimal(str(sale_price))
        update_fields.append('sale_price')
    if branch_only is not None:
        product.branch_only = bool(branch_only)
        update_fields.append('branch_only')
    if in_stock is not None and not product.has_per_size_stock():
        if not in_stock and int(product.stock_quantity or 0) > 0:
            product.stock_quantity = 0
            update_fields.append('stock_quantity')
    if image_url:
        normalized = normalize_website_image_url(image_url)
        if normalized:
            product.image_url = normalized
            update_fields.append('image_url')

    if not update_fields:
        return product

    update_fields.append('updated_at')
    product.save(update_fields=update_fields)
    return product


def sync_products_from_website() -> dict:
    """
    Import/update CRM store products from the B2C public shop catalog.
    Creates missing products; links by website legacy_id; pushes CRM stock/price back.
    """
    if not _integration_configured():
        raise ValueError('Website integration is not configured (WEBSITE_INTEGRATION_URL / API key)')

    base = settings.WEBSITE_INTEGRATION_URL.rstrip('/')
    created = 0
    updated = 0
    errors: list[str] = []
    to_push: list[StoreProduct] = []

    for brand in ('cogo', 'gaga'):
        try:
            resp = requests.get(f'{base}/api/products?brand={brand}', timeout=45)
            resp.raise_for_status()
            items = resp.json().get('products') or []
        except requests.RequestException as exc:
            errors.append(f'{brand}: fetch failed ({exc})')
            continue

        default_category = 'געגע' if brand == 'gaga' else 'קוגומלו'

        for wp in items:
            try:
                legacy_id = int(wp['id'])
            except (KeyError, TypeError, ValueError):
                continue

            name = (wp.get('name') or '').strip()
            if not name:
                continue

            price = Decimal(str(wp.get('price') or 0))
            in_stock = bool(wp.get('inStock', True))
            images = wp.get('images') or []
            image_url = normalize_website_image_url((images[0] if images else '') or '')
            categories = wp.get('categories') or []
            category = (categories[0] if categories else default_category) or default_category
            branch_only = not bool(wp.get('purchasable', True))

            product = StoreProduct.objects.filter(website_legacy_id=legacy_id).first()
            is_new = product is None
            if is_new:
                product = (
                    StoreProduct.objects.filter(
                        website_legacy_id__isnull=True,
                        name=name,
                        is_active=True,
                    ).first()
                )

            if product:
                # Only touch catalog metadata — never sale_price on existing rows.
                # Use update_fields so a concurrent staff price edit is not overwritten
                # by a stale in-memory sale_price from when this row was loaded.
                product.name = name
                product.website_legacy_id = legacy_id
                product.category = category
                product.branch = None
                product.is_active = True
                product.branch_only = branch_only
                update_fields = [
                    'name', 'website_legacy_id', 'category', 'branch',
                    'is_active', 'branch_only', 'updated_at',
                ]
                if image_url:
                    product.image_url = image_url
                    update_fields.append('image_url')
                product.save(update_fields=update_fields)
                updated += 1
            else:
                sale = price if price >= Decimal('0.01') else Decimal('0.01')
                cost = Decimal('0.00')
                qty = 10 if in_stock else 0
                product = StoreProduct.objects.create(
                    name=name,
                    category=category,
                    sale_price=sale,
                    cost_price=cost,
                    stock_quantity=qty,
                    website_legacy_id=legacy_id,
                    image_url=image_url,
                    branch=None,
                    is_active=True,
                    branch_only=branch_only,
                    notes=f'יובא מהאתר ({brand})',
                )
                created += 1

            to_push.append(product)

    pushed = push_products_batch_to_website(to_push)

    return {
        'ok': True,
        'created': created,
        'updated': updated,
        'pushed': pushed,
        'errors': errors,
        'total_crm': StoreProduct.objects.filter(is_active=True).count(),
    }


def notify_website_order_status(
    *,
    website_order_number: str,
    invoice_number: str,
    invoice_id: str,
    status: str,
    provider_txn_id: str = '',
) -> bool:
    """
    Tell the B2C site that a website order was paid or failed (after Tranzila webhook).
    POST /api/integrations/order-paid on the public shop.
    """
    if not _integration_configured() or not website_order_number:
        return False

    url = settings.WEBSITE_INTEGRATION_URL.rstrip('/') + '/api/integrations/order-paid'
    headers = {
        'Authorization': f'Bearer {settings.WEBSITE_INTEGRATION_API_KEY}',
        'Content-Type': 'application/json',
    }
    payload = {
        'website_order_number': website_order_number,
        'invoice_number': invoice_number,
        'invoice_id': invoice_id,
        'status': status,
        'provider_txn_id': provider_txn_id or None,
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        if resp.status_code >= 400:
            logger.error(
                'Website order status push failed: HTTP %s order=%s body=%s',
                resp.status_code,
                website_order_number,
                resp.text[:500],
            )
            return False
        logger.info('Notified website order %s → %s', website_order_number, status)
        return True
    except requests.RequestException as exc:
        logger.warning('Website order status push error for %s: %s', website_order_number, exc)
        return False
