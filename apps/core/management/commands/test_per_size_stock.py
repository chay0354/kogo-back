from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.store.models import StoreProduct
from apps.store.serializers import StoreProductSerializer
from apps.store.stock_utils import decrement_product_stock, restore_stock_for_sale


class RollbackDemo(Exception):
    """Internal sentinel used to rollback all demo data."""


class Command(BaseCommand):
    help = (
        "Verifies per-size stock control end-to-end: create a product with two "
        "sizes, decrement one size via the sale helper, restore it via the "
        "refund helper, and confirm the per-size and total quantities update."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep-data",
            action="store_true",
            help="Do not rollback the temporary product after the check.",
        )

    def handle(self, *args, **options):
        keep_data = options["keep_data"]

        try:
            with transaction.atomic():
                self._run_check()
                if keep_data:
                    self.stdout.write(
                        self.style.WARNING("Temporary test data was kept because --keep-data was used.")
                    )
                    return
                raise RollbackDemo
        except RollbackDemo:
            self.stdout.write(self.style.WARNING("Temporary test data rolled back."))

    def _run_check(self):
        serializer = StoreProductSerializer(data={
            "name": "Per-Size Stock Test Tee",
            "category": "בדיקה",
            "cost_price": "20.00",
            "sale_price": "50.00",
            "branch": None,
            "stock_quantity": 0,
            "min_stock_alert": 1,
            "size_stocks": [
                {"size": "S", "stock_quantity": 5},
                {"size": "M", "stock_quantity": 3},
            ],
        })
        serializer.is_valid(raise_exception=True)
        product: StoreProduct = serializer.save()

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Per-size stock check"))
        self._print_state("Initial", product)

        decrement_product_stock(product, {"product_id": str(product.id), "quantity": 2, "size": "S"})
        product.refresh_from_db()
        self._print_state("After selling 2x size S", product)

        s_row = product.size_stocks.filter(size="S").order_by("sort_order").first()
        m_row = product.size_stocks.filter(size="M").order_by("sort_order").first()
        if s_row.stock_quantity != 3 or m_row.stock_quantity != 3:
            raise AssertionError(
                f"Expected S=3 / M=3 after sale, got S={s_row.stock_quantity} M={m_row.stock_quantity}"
            )
        if product.stock_quantity != 6:
            raise AssertionError(f"Expected total stock 6 after sale, got {product.stock_quantity}")

        sale_like = type("SaleLike", (), {
            "product_id": product.id,
            "product": product,
            "quantity": 2,
            "size": "S",
            "branch_id": None,
        })()
        restore_stock_for_sale(sale_like)
        product.refresh_from_db()
        self._print_state("After refund (restore 2x size S)", product)

        s_row.refresh_from_db()
        if s_row.stock_quantity != 5:
            raise AssertionError(f"Expected S to return to 5 after refund, got {s_row.stock_quantity}")
        if product.stock_quantity != 8:
            raise AssertionError(f"Expected total stock 8 after refund, got {product.stock_quantity}")

        self.stdout.write(self.style.SUCCESS("PASS: per-size stock decrement and refund work correctly."))

    def _print_state(self, label: str, product: StoreProduct) -> None:
        rows = ", ".join(
            f"{row.size}={row.stock_quantity}" for row in product.size_stocks.order_by("sort_order", "size")
        )
        self.stdout.write(f"{label}: total={product.stock_quantity}  sizes=[{rows}]")
