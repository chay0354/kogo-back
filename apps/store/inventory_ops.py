"""
Shared inventory mutations for staff store APIs and the B2C integration API.

Adjust / transfer / size-row replace must stay identical in both UIs so a
change made on the website is the same operation the CRM already audits.
"""
from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from rest_framework import serializers as drf_serializers

from apps.store.models import InventoryAdjustment, StoreProduct, StoreProductSize
from apps.store.serializers import StoreProductSerializer
from apps.store.website_integration import product_in_stock

logger = logging.getLogger(__name__)


class InventoryError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status
        super().__init__(message)


def reload_product(product: StoreProduct) -> StoreProduct:
    return (
        StoreProduct.objects
        .select_related('branch')
        .prefetch_related('size_stocks__branch')
        .get(pk=product.pk)
    )


def flatten_errors(errors: Any) -> str:
    if isinstance(errors, dict):
        parts = []
        for key, value in errors.items():
            inner = flatten_errors(value)
            parts.append(inner if key == 'non_field_errors' else f'{key}: {inner}')
        return '; '.join(parts)
    if isinstance(errors, (list, tuple)):
        return flatten_errors(errors[0]) if errors else ''
    return str(errors)


def serialize_size_stock(row: StoreProductSize) -> dict:
    branch_name = None
    if row.branch_id:
        branch_name = getattr(row.branch, 'name', None)
    return {
        'id': str(row.id),
        'size': row.size,
        'stock_quantity': int(row.stock_quantity or 0),
        'sort_order': int(row.sort_order or 0),
        'branch': str(row.branch_id) if row.branch_id else None,
        'branch_name': branch_name,
    }


def serialize_integration_product(p: StoreProduct) -> dict:
    """Catalog + full inventory payload for the B2C integration API."""
    size_stocks = []
    sizes = []
    for row in p.size_stocks.all():
        size_stocks.append(serialize_size_stock(row))
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
        'delivery_price': str(p.delivery_price or 0),
        'image_url': p.image_url or '',
        'stock_quantity': int(p.stock_quantity or 0),
        'min_stock_alert': int(p.min_stock_alert or 0),
        'is_low_stock': bool(p.is_low_stock),
        'in_stock': product_in_stock(p),
        'website_legacy_id': p.website_legacy_id,
        'branch_only': p.branch_only,
        'branch': str(p.branch_id) if p.branch_id else None,
        'branch_name': getattr(p.branch, 'name', None) if p.branch_id else None,
        'sizes': sizes,
        'size_stocks': size_stocks,
    }


def adjust_product_stock(
    product: StoreProduct,
    *,
    quantity_delta: int,
    reason: str,
    note: str = '',
    size_stock_id: str | None = None,
    user=None,
) -> StoreProduct:
    """Add or subtract stock with an InventoryAdjustment audit row."""
    valid_reasons = {r[0] for r in InventoryAdjustment.REASON_CHOICES}
    if reason not in valid_reasons:
        raise InventoryError(f'Invalid reason. Must be one of: {", ".join(sorted(valid_reasons))}')

    size_stock_id = (size_stock_id or '').strip()

    with transaction.atomic():
        size_row = None
        if size_stock_id and product.has_per_size_stock():
            size_row = (
                StoreProductSize.objects
                .select_for_update()
                .filter(product=product, pk=size_stock_id)
                .first()
            )
            if size_row is None:
                raise InventoryError('Size stock row not found for this product')

        if size_row is not None:
            new_qty = max(0, size_row.stock_quantity + quantity_delta)
            actual_delta = new_qty - size_row.stock_quantity
            size_row.stock_quantity = new_qty
            size_row.save(update_fields=['stock_quantity', 'updated_at'])
            product.recalculate_total_stock()
        else:
            new_qty = max(0, product.stock_quantity + quantity_delta)
            actual_delta = new_qty - product.stock_quantity
            product.stock_quantity = new_qty
            product.save(update_fields=['stock_quantity', 'updated_at'])

        InventoryAdjustment.objects.create(
            product=product,
            size_stock=size_row,
            quantity_delta=actual_delta,
            reason=reason,
            note=note or '',
            adjusted_by=user if getattr(user, 'is_authenticated', False) else None,
        )

    logger.info(
        "Inventory adjustment for %s: delta=%s, reason=%s, size_stock=%s",
        product.name, actual_delta, reason, size_stock_id or '-',
    )
    return reload_product(product)


def transfer_product_stock(
    product: StoreProduct,
    *,
    quantity: int,
    from_size_stock_id: str,
    to_size_stock_id: str,
) -> StoreProduct:
    """Move units between two size/location rows of the same product."""
    if quantity <= 0:
        raise InventoryError('quantity must be greater than 0')

    from_id = (from_size_stock_id or '').strip()
    to_id = (to_size_stock_id or '').strip()
    if not from_id or not to_id:
        raise InventoryError('from_size_stock_id and to_size_stock_id are required')
    if from_id == to_id:
        raise InventoryError('Source and destination cannot be the same row')

    with transaction.atomic():
        rows = (
            StoreProductSize.objects
            .select_for_update()
            .filter(product=product, pk__in=[from_id, to_id])
        )
        row_map = {str(r.pk): r for r in rows}

        if from_id not in row_map:
            raise InventoryError('Source size stock row not found')
        if to_id not in row_map:
            raise InventoryError('Destination size stock row not found')

        from_row = row_map[from_id]
        to_row = row_map[to_id]

        if from_row.stock_quantity < quantity:
            raise InventoryError(
                f'מלאי לא מספיק במקור (נוכחי: {from_row.stock_quantity}, מבוקש: {quantity})'
            )

        from_row.stock_quantity -= quantity
        to_row.stock_quantity += quantity
        from_row.save(update_fields=['stock_quantity', 'updated_at'])
        to_row.save(update_fields=['stock_quantity', 'updated_at'])
        product.recalculate_total_stock()

    logger.info(
        "Stock transfer for %s: quantity=%s, from=%s, to=%s",
        product.name, quantity, from_id, to_id,
    )
    return reload_product(product)


def save_product_inventory(product: StoreProduct, payload: dict) -> StoreProduct:
    """
    Update min-stock alert, flat stock_quantity, and/or replace size_stocks.

    Only inventory fields are accepted — price and catalog fields stay on the
    existing integration/update endpoint so this path cannot rewrite sale price.
    """
    allowed: dict[str, Any] = {}
    if 'min_stock_alert' in payload:
        allowed['min_stock_alert'] = payload['min_stock_alert']
    if 'stock_quantity' in payload:
        allowed['stock_quantity'] = payload['stock_quantity']
    if 'size_stocks' in payload:
        allowed['size_stocks'] = payload['size_stocks']
    if 'branch' in payload and 'size_stocks' not in payload:
        branch = payload.get('branch')
        allowed['branch'] = None if branch in ('', 'delivery', None) else branch

    if not allowed:
        raise InventoryError('No inventory fields to update')

    ser = StoreProductSerializer(product, data=allowed, partial=True)
    if not ser.is_valid():
        raise InventoryError(flatten_errors(ser.errors))
    try:
        product = ser.save()
    except drf_serializers.ValidationError as exc:
        raise InventoryError(flatten_errors(exc.detail)) from exc
    return reload_product(product)
