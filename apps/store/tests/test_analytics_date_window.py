"""The store analytics window includes both of its dates, in Asia/Jerusalem."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.store.models import StoreInvoice, StoreProduct, StoreSale

User = get_user_model()


class StoreAnalyticsDateWindowTest(TestCase):
    def setUp(self):
        user = User.objects.create_user(username='store-mgr', password='x', is_staff=True)
        profile = getattr(user, 'profile', None)
        if profile is not None:
            profile.role = 'manager'
            profile.save()
        self.client = APIClient()
        self.client.force_authenticate(user)
        self.product = StoreProduct.objects.create(
            name='חולצה', category='ביגוד', cost_price=Decimal('20'), sale_price=Decimal('50'), stock_quantity=10,
        )
        self.invoice = StoreInvoice.objects.create(
            customer_name='אורח', total_amount=Decimal('50'), payment_method='cash', payment_status='completed',
        )
        self.today = timezone.localdate()

    def _sale_at(self, day, hour):
        sale = StoreSale.objects.create(
            invoice=self.invoice, product=self.product, quantity=1,
            unit_price=Decimal('50'), total_price=Decimal('50'), payment_method='cash',
        )
        moment = timezone.make_aware(datetime.combine(day, time(hour, 0)))
        StoreSale.objects.filter(pk=sale.pk).update(sale_date=moment)
        return sale

    def test_a_sale_made_during_the_last_day_of_the_window_is_counted(self):
        self._sale_at(self.today, 12)
        self._sale_at(self.today + timedelta(days=1), 9)  # tomorrow: outside the window
        response = self.client.get('/api/v1/store/sales/analytics/', {
            'date_from': (self.today - timedelta(days=6)).isoformat(),
            'date_to': self.today.isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total_sales_count'], 1)
        self.assertEqual(response.data['total_revenue'], 50.0)
