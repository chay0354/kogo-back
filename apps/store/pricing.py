"""
Line totals for store checkout, including optional delivery.

`delivery_price` lives on `StoreProduct`. Website checkout charges it once
per order (the max unit fee among items). Staff POS still adds it per line.
Branch pickup does not add the fee.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from apps.store.models import StoreProduct


def cart_item_is_delivery(item: Mapping, product: StoreProduct) -> bool:
    """True when this line ships (משלוח), not when it is picked up at a branch."""
    if 'branch' in item:
        return item.get('branch') in (None, '', 'delivery')
    return product.branch_id is None


def delivery_unit_price(product: StoreProduct) -> Decimal:
    return Decimal(str(getattr(product, 'delivery_price', 0) or 0))


def line_delivery_amount(product: StoreProduct, quantity: int, item: Mapping | None = None) -> Decimal:
    fee = delivery_unit_price(product)
    if fee <= 0:
        return Decimal('0.00')
    if item is not None and not cart_item_is_delivery(item, product):
        return Decimal('0.00')
    return (fee * Decimal(quantity)).quantize(Decimal('0.01'))


def line_product_amount(product: StoreProduct, quantity: int) -> Decimal:
    return (product.sale_price * Decimal(quantity)).quantize(Decimal('0.01'))


def line_charge_amount(product: StoreProduct, quantity: int, item: Mapping | None = None) -> Decimal:
    return line_product_amount(product, quantity) + line_delivery_amount(product, quantity, item)


def order_delivery_amount(products: list[StoreProduct], *, is_delivery: bool) -> Decimal:
    """Website shipping: once per order, not per product or unit."""
    if not is_delivery:
        return Decimal('0.00')
    fees = [delivery_unit_price(product) for product in products]
    fees = [fee for fee in fees if fee > 0]
    if not fees:
        return Decimal('0.00')
    return max(fees).quantize(Decimal('0.01'))


def sale_unit_and_total(product: StoreProduct, item: Mapping) -> tuple[Decimal, Decimal]:
    """Unit/total stored on StoreSale so invoice lines match the charged amount."""
    quantity = int(item.get('quantity') or 0)
    extra = Decimal('0.00')
    # Website checkout bills shipping once on the invoice, not on each sale line.
    if item.get('line_delivery') is not False and cart_item_is_delivery(item, product):
        extra = delivery_unit_price(product)
    unit = (product.sale_price + extra).quantize(Decimal('0.01'))
    return unit, (unit * Decimal(quantity)).quantize(Decimal('0.01'))


def tranzila_items_for_cart_line(product: StoreProduct, item: Mapping) -> list[dict]:
    """Product row plus a separate delivery row when a shipping fee applies."""
    quantity = int(item.get('quantity') or 0)
    rows = [{
        'name': f"{product.name} {item.get('size', '')}".strip(),
        'type': 'I',
        'unit_price': float(product.sale_price),
        'units_number': quantity,
        'unit_type': 1,
        'price_type': 'G',
        'currency_code': 'ILS',
    }]
    delivery = line_delivery_amount(product, quantity, item)
    if delivery > 0:
        rows.append({
            'name': f'משלוח — {product.name}',
            'type': 'I',
            'unit_price': float(delivery_unit_price(product)),
            'units_number': quantity,
            'unit_type': 1,
            'price_type': 'G',
            'currency_code': 'ILS',
        })
    return rows
