from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.core.payment_service import resolve_billing_price, PaymentService
from apps.core.tests.test_fixtures import TestDataFactory
from apps.courses.models import LessonPriceOption
from apps.customers.discount_service import DiscountCalculation
from apps.customers.models import Payment


class LessonPriceOptionBillingTest(TestCase):
    def setUp(self):
        self.child = TestDataFactory.create_child()
        self.lesson = TestDataFactory.create_lesson()
        self.price_option = LessonPriceOption.objects.create(
            lesson=self.lesson,
            display_title='VIP track',
            monthly_price=Decimal('350.00'),
            sort_order=1,
        )
        self.service = PaymentService()

    def test_resolve_billing_price_uses_catalog_option(self):
        base_price, used_tier, _, bundle, option = resolve_billing_price(
            self.child,
            self.lesson,
            price_option_id=str(self.price_option.id),
        )
        self.assertEqual(base_price, Decimal('350.00'))
        self.assertTrue(used_tier)
        self.assertIsNone(bundle)
        self.assertEqual(option, self.price_option)

    @patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    def test_initiate_subscription_payment_stores_price_option(self, mock_discount, mock_tranzila):
        mock_discount.return_value = DiscountCalculation(
            applicable_discounts=[],
            total_discount_amount=Decimal('0.00'),
            final_price=Decimal('350.00'),
            base_price=Decimal('350.00'),
        )
        mock_tranzila.return_value = 'https://tranzila.test/payment'

        result = self.service.initiate_subscription_payment(
            child_id=str(self.child.id),
            lesson_id=str(self.lesson.id),
            price_option_id=str(self.price_option.id),
        )

        payment = Payment.objects.get(id=result['payment_id'])
        self.assertEqual(payment.price_option_id, self.price_option.id)
        self.assertEqual(payment.base_amount, Decimal('350.00'))
