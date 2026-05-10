from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.store.models import StoreProduct, StoreProductSize


class RollbackDemo(Exception):
    pass


class Command(BaseCommand):
    help = (
        "Verifies that a product whose stock_quantity is <= min_stock_alert is "
        "counted in the store analytics low_stock_count and exposes "
        "is_low_stock=True via the API."
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
        User = get_user_model()
        manager = User.objects.create_user(
            username="low-stock-tester@kogo.test",
            email="low-stock-tester@kogo.test",
            password="pass12345!",
            is_active=True,
        )
        UserProfile.objects.update_or_create(
            user=manager,
            defaults={"role": UserProfile.ROLE_MANAGER},
        )
        token = Token.objects.create(user=manager)
        client = APIClient(HTTP_HOST="testserver")
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        from django.conf import settings as _settings

        if "testserver" not in _settings.ALLOWED_HOSTS:
            _settings.ALLOWED_HOSTS = list(_settings.ALLOWED_HOSTS) + ["testserver"]

        baseline = client.get("/api/v1/store/sales/analytics/?days=30").json()
        baseline_count = int(baseline.get("low_stock_count", 0))
        self.stdout.write(f"Baseline low_stock_count: {baseline_count}")

        equal_product = StoreProduct.objects.create(
            name="Low Stock Equal Tester",
            category="בדיקה",
            cost_price=Decimal("10.00"),
            sale_price=Decimal("30.00"),
            stock_quantity=5,
            min_stock_alert=5,
            is_active=True,
        )
        below_product = StoreProduct.objects.create(
            name="Low Stock Below Tester",
            category="בדיקה",
            cost_price=Decimal("10.00"),
            sale_price=Decimal("30.00"),
            stock_quantity=2,
            min_stock_alert=5,
            is_active=True,
        )

        if not equal_product.is_low_stock:
            raise AssertionError("Expected stock_quantity == min_stock_alert to be low_stock=True")
        if not below_product.is_low_stock:
            raise AssertionError("Expected stock_quantity < min_stock_alert to be low_stock=True")

        size_product = StoreProduct.objects.create(
            name="Low Stock Per Size Tester",
            category="בדיקה",
            cost_price=Decimal("10.00"),
            sale_price=Decimal("30.00"),
            stock_quantity=0,
            min_stock_alert=4,
            is_active=True,
        )
        StoreProductSize.objects.create(product=size_product, size="S", stock_quantity=2, sort_order=0)
        StoreProductSize.objects.create(product=size_product, size="M", stock_quantity=2, sort_order=1)
        size_product.recalculate_total_stock()
        size_product.refresh_from_db()
        if size_product.stock_quantity != 4:
            raise AssertionError(
                f"Expected sum of size stocks to be 4, got {size_product.stock_quantity}"
            )
        if not size_product.is_low_stock:
            raise AssertionError(
                "Expected per-size product (4 units, alert=4) to be low_stock=True"
            )

        after = client.get("/api/v1/store/sales/analytics/?days=30").json()
        after_count = int(after.get("low_stock_count", 0))
        self.stdout.write(f"After-create low_stock_count: {after_count}")

        new_low_stock_ids = {row["id"] for row in after.get("low_stock_products", [])}
        for product in (equal_product, below_product, size_product):
            if str(product.id) not in new_low_stock_ids:
                raise AssertionError(
                    f"Product {product.name} ({product.id}) missing from low_stock_products list"
                )

        delta = after_count - baseline_count
        if delta != 3:
            raise AssertionError(
                f"Expected low_stock_count to increase by 3, increased by {delta}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                "PASS: stock_quantity <= min_stock_alert (including per-size products) "
                "increments the dashboard low_stock_count."
            )
        )
