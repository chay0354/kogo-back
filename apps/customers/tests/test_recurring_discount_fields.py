"""
The customer's screen has to be able to say why a standing order costs what it
costs. That answer lives in three fields the API never sent: the price before
discounts, what was taken off, and which discounts did it.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import RecurringPayment
from apps.customers.serializers import RecurringPaymentSerializer


class RecurringDiscountFieldsTests(TestCase):
    def _recurring(self, **kwargs):
        family = TestDataFactory.create_family()
        TestDataFactory.create_parent(family=family)
        child = TestDataFactory.create_child(family=family)
        defaults = dict(
            child=child,
            base_amount=Decimal('260.00'),
            discount_amount=Decimal('35.00'),
            amount=Decimal('225.00'),
            discount_details=[{'name': 'הנחת אח שני', 'type': 'percent', 'value': '10'}],
            status='active',
            start_date=date(2026, 9, 1),
        )
        defaults.update(kwargs)
        return RecurringPayment.objects.create(**defaults)

    def test_the_price_before_and_after_the_discount_both_reach_the_screen(self):
        data = RecurringPaymentSerializer(self._recurring()).data
        self.assertEqual(Decimal(data['base_amount']), Decimal('260.00'))
        self.assertEqual(Decimal(data['discount_amount']), Decimal('35.00'))
        self.assertEqual(Decimal(data['amount']), Decimal('225.00'))

    def test_the_discounts_themselves_are_named(self):
        data = RecurringPaymentSerializer(self._recurring()).data
        self.assertEqual(data['discount_details'][0]['name'], 'הנחת אח שני')

    def test_an_order_with_no_discount_says_nothing_rather_than_breaking(self):
        data = RecurringPaymentSerializer(
            self._recurring(discount_amount=Decimal('0.00'), discount_details=[], amount=Decimal('260.00'))
        ).data
        self.assertEqual(Decimal(data['discount_amount']), Decimal('0.00'))
        self.assertEqual(data['discount_details'], [])

    def test_the_new_fields_cannot_be_written_through_the_api(self):
        serializer = RecurringPaymentSerializer()
        for field in ('base_amount', 'discount_amount', 'discount_details'):
            self.assertTrue(serializer.fields[field].read_only, field)
