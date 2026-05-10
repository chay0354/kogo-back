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


def _resolve_size_row(product: StoreProduct, size: str) -> StoreProductSize | None:
    if not size:
        return None
    return (
        StoreProductSize.objects
        .select_for_update()
        .filter(product=product, size=size)
        .first()
    )


def decrement_product_stock(product: StoreProduct, item: Mapping) -> None:
    """
    Decrement stock for a sale `item` (`{product_id, quantity, size?}`).

    When the product tracks stock per size and `item['size']` matches an
    existing size row, that row is decremented and the product total is
    recomputed from all size rows. Otherwise we fall back to decrementing the
    product's flat `stock_quantity`, preserving the legacy behaviour.

    Caller is expected to be inside an atomic block; the caller has also
    already validated stock availability.
    """
    quantity = int(item.get('quantity', 0))
    if quantity <= 0:
        return

    size = (item.get('size') or '').strip()

    with transaction.atomic():
        if size and product.has_per_size_stock():
            size_row = _resolve_size_row(product, size)
            if size_row is None:
                logger.warning(
                    "decrement_product_stock: size %s not found for product %s; "
                    "falling back to total stock decrement",
                    size, product.id,
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

    If the sale recorded a `size` and the product still has that size row,
    the row is incremented and the product total is recomputed; otherwise the
    flat `stock_quantity` is incremented.
    """
    quantity = int(sale.quantity or 0)
    if quantity <= 0:
        return

    size = (sale.size or '').strip()
    product = StoreProduct.objects.select_for_update().get(pk=sale.product_id)

    with transaction.atomic():
        if size and product.has_per_size_stock():
            size_row = _resolve_size_row(product, size)
            if size_row is not None:
                size_row.stock_quantity = max(0, size_row.stock_quantity + quantity)
                size_row.save(update_fields=['stock_quantity', 'updated_at'])
                product.recalculate_total_stock()
                return

        StoreProduct.objects.filter(pk=product.pk).update(
            stock_quantity=F('stock_quantity') + quantity,
        )
        product.refresh_from_db(fields=['stock_quantity'])
