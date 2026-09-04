"""
Stock mutation helpers for store products.

Centralizes all decrement/restore logic so per-size aware stock is updated
consistently across:
- direct card charges (apps/store/views.py)
- token charges and webhook completions (apps/core/payment_service.py)
- cash / monthly billing invoices (apps/core/payment_service.py)
- refunds (apps/core/payment_service.py)
"""
from __future__ import annotations

import logging
from typing import Mapping

from django.db import transaction
from django.db.models import F

from apps.store.models import StoreProduct, StoreProductSize, StoreSale

logger = logging.getLogger(__name__)


def _size_row_qs(product: StoreProduct, item: Mapping, *, for_update: bool = False):
    """Queryset for the size row matching a cart / sale item."""
    qs = StoreProductSize.objects.filter(product=product)
    if for_update:
        qs = qs.select_for_update()

    size_stock_id = str(item.get('size_stock_id') or '').strip()
    if size_stock_id:
        return qs.filter(pk=size_stock_id)

    size = (item.get('size') or '').strip()
    if size:
        return qs.filter(size=size)
    # A product without sizes still keeps its stock per location: one row per
    # place, with an empty size. Without this the line fell back to the single
    # product total, and "pickup" quietly drew on the delivery pool.
    return qs.filter(size='')


def peek_size_row(product: StoreProduct, item: Mapping) -> StoreProductSize | None:
    """Non-locking lookup of the size row a cart line would decrement."""
    return _match_size_row(_size_row_qs(product, item, for_update=False), item)


def _resolve_size_row(product: StoreProduct, item: Mapping) -> StoreProductSize | None:
    """
    Pick the `StoreProductSize` row to mutate for a cart / sale item.

    Prefer `size_stock_id` when present. Otherwise match `size` and optional
    `branch` (UUID or null for משלוח). Fall back to the first row for that size
    (legacy clients that only send size).
    """
    return _match_size_row(_size_row_qs(product, item, for_update=True), item)


def _match_size_row(qs, item: Mapping) -> StoreProductSize | None:
    size_stock_id = str(item.get('size_stock_id') or '').strip()
    if size_stock_id:
        return qs.first()

    if 'branch' in item:
        br = item.get('branch')
        if br in (None, '', 'delivery'):
            return qs.filter(branch__isnull=True).order_by('sort_order').first()
        bid = str(br).strip()
        return qs.filter(branch_id=bid).order_by('sort_order').first()

    return qs.order_by('sort_order').first()


def tracks_stock_by_location(product: StoreProduct) -> bool:
    """
    Whether this product's stock is held in location rows.

    True as soon as the product has any row — with sizes or without. A product
    with no rows at all is the old shape, where a single number stands for
    every location; it is answered from that number until its stock is placed.
    """
    return product.has_per_size_stock()


def available_stock_for_item(product: StoreProduct, item: Mapping) -> int:
    """Units available for this line at the requested location (delivery vs branch)."""
    if tracks_stock_by_location(product):
        row = peek_size_row(product, item)
        if row is None:
            return 0
        return int(row.stock_quantity or 0)
    return int(product.stock_quantity or 0)


def store_line_item_branch_id(item: Mapping, product: StoreProduct):
    """Branch FK for `StoreSale` / reporting: prefer explicit line item branch, else product default."""
    br = item.get('branch')
    if br not in (None, '', 'delivery'):
        s = str(br).strip()
        if s:
            return s
    return product.branch_id


def decrement_product_stock(product: StoreProduct, item: Mapping) -> None:
    """
    Decrement stock for a sale `item` (`{product_id, quantity, size?, branch?, size_stock_id?}`).

    When the product tracks stock per size and a matching size row is found,
    that row is decremented and the product total is recomputed. Otherwise we
    fall back to decrementing the product's flat `stock_quantity`.
    """
    quantity = int(item.get('quantity', 0))
    if quantity <= 0:
        return

    size = (item.get('size') or '').strip()

    with transaction.atomic():
        if tracks_stock_by_location(product):
            size_row = _resolve_size_row(product, item)
            if size_row is None:
                logger.warning(
                    "decrement_product_stock: no size row for product %s item=%s; "
                    "falling back to total stock decrement",
                    product.id,
                    {k: item.get(k) for k in ('size', 'branch', 'size_stock_id')},
                )
            else:
                size_row.stock_quantity = max(0, size_row.stock_quantity - quantity)
                size_row.save(update_fields=['stock_quantity', 'updated_at'])
                product.recalculate_total_stock()
                return

        StoreProduct.objects.filter(pk=product.pk).update(
            stock_quantity=F('stock_quantity') - quantity,
        )
        product.refresh_from_db(fields=['stock_quantity'])


def restore_stock_for_sale(sale: StoreSale) -> None:
    """
    Add the units from a refunded `StoreSale` back into stock.

    If the sale recorded a `size` and the product still has a matching size row
    (using sale branch when available), that row is incremented; otherwise the
    flat `stock_quantity` is incremented.
    """
    quantity = int(sale.quantity or 0)
    if quantity <= 0:
        return

    size = (sale.size or '').strip()
    product = StoreProduct.objects.select_for_update().get(pk=sale.product_id)

    item: dict = {'size': size}
    if sale.branch_id:
        item['branch'] = str(sale.branch_id)

    with transaction.atomic():
        if size and product.has_per_size_stock():
            size_row = _resolve_size_row(product, item)
            if size_row is not None:
                size_row.stock_quantity = max(0, size_row.stock_quantity + quantity)
                size_row.save(update_fields=['stock_quantity', 'updated_at'])
                product.recalculate_total_stock()
                return

        StoreProduct.objects.filter(pk=product.pk).update(
            stock_quantity=F('stock_quantity') + quantity,
        )
        product.refresh_from_db(fields=['stock_quantity'])
