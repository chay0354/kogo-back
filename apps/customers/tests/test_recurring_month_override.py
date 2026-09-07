"""Billing one month differently, and putting a store purchase on that month."""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Payment, RecurringChargeOverride, RecurringPayment
from apps.customers.recurring_amount import (
    add_to_month_override,
    amount_for_charge,
    clear_month_override,
    set_month_override,
)
from apps.customers.recurring_billing import process_due_recurring_charges


def _ok(**over):
    result = {
        'success': True,
        'transaction_id': 'txn-1',
        'confirmation_code': '0000000',
        'response_code': '000',
        'raw_response': {},
    }
    result.update(over)
    return result


class RecurringMonthOverrideTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='manager-override@test.com',
            email='manager-override@test.com',
            password='pass12345!',
        )
        UserProfile.objects.update_or_create(
            user=self.user, defaults={'role': UserProfile.ROLE_MANAGER}
        )

        self.today = timezone.localdate()
        self.this_month = self.today.replace(day=1)

        self.course = TestDataFactory.create_course(price=Decimal('275.00'))
        self.lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.family = TestDataFactory.create_family(phone='0500000111')
        self.child = TestDataFactory.create_child(family=self.family, first_name='יובל')
        self.payment = Payment.objects.create(
            child=self.child,
            family=self.family,
            branch=self.course.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('275.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('275.00'),
        )
        self.recurring = RecurringPayment.objects.create(
            child=self.child,
            initial_payment=self.payment,
            status='active',
            amount=Decimal('275.00'),
            base_amount=Decimal('275.00'),
            billing_day=1,
            start_date=self.this_month,
            next_billing_date=self.today,
            tranzila_token='tok-override',
            card_expire_month=12,
            card_expire_year=2030,
        )

    # ---- the amount the cron will take -------------------------------------

    def test_no_override_bills_the_regular_amount(self):
        amount, override = amount_for_charge(self.recurring, on_date=self.today)
        self.assertEqual(amount, Decimal('275.00'))
        self.assertIsNone(override)

    def test_override_replaces_the_amount_for_its_month_only(self):
        set_month_override(
            self.recurring,
            billing_month=self.this_month,
            amount=Decimal('150.00'),
            reason='רק שני שיעורים החודש',
            created_by=self.user,
        )
        amount, override = amount_for_charge(self.recurring, on_date=self.today)
        self.assertEqual(amount, Decimal('150.00'))
        self.assertIsNotNone(override)

        next_month = (self.this_month + timedelta(days=32)).replace(day=1)
        later, later_override = amount_for_charge(self.recurring, on_date=next_month)
        self.assertEqual(later, Decimal('275.00'))
        self.assertIsNone(later_override)

    # ---- what the cron actually charges -------------------------------------

    @patch('apps.customers.recurring_billing.TranzilaService')
    def test_cron_charges_the_override_and_spends_it(self, tranzila_cls):
        tranzila_cls.production.return_value.charge_with_token.return_value = _ok()
        set_month_override(
            self.recurring,
            billing_month=self.this_month,
            amount=Decimal('150.00'),
            reason='חודש קצר',
            created_by=self.user,
        )

        summary = process_due_recurring_charges()

        self.assertEqual(summary['charged'], 1)
        charged = tranzila_cls.production.return_value.charge_with_token.call_args
        self.assertEqual(charged.kwargs['amount'], Decimal('150.00'))

        override = RecurringChargeOverride.objects.get(recurring_payment=self.recurring)
        self.assertIsNotNone(override.applied_at)

        payment = Payment.objects.filter(payment_type='recurring_subscription').latest('created_at')
        self.assertEqual(payment.final_amount, Decimal('150.00'))

    @patch('apps.customers.recurring_billing.TranzilaService')
    def test_a_customer_with_no_override_is_charged_exactly_as_before(self, tranzila_cls):
        """The regression guard for everyone already on a standing order."""
        tranzila_cls.production.return_value.charge_with_token.return_value = _ok()

        summary = process_due_recurring_charges()

        self.assertEqual(summary['charged'], 1)
        call = tranzila_cls.production.return_value.charge_with_token.call_args
        self.assertEqual(call.kwargs['amount'], Decimal('275.00'))
        # The line item Tranzila is shown must carry the same figure as the charge.
        self.assertEqual(call.kwargs['items'][0]['unit_price'], 275.0)

        payment = Payment.objects.filter(payment_type='recurring_subscription').latest('created_at')
        self.assertEqual(payment.final_amount, Decimal('275.00'))
        self.assertEqual(payment.status, 'completed')

        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.amount, Decimal('275.00'))
        self.assertEqual(self.recurring.last_charge_date, self.today)
        self.assertFalse(RecurringChargeOverride.objects.exists())

    @patch('apps.customers.recurring_billing.TranzilaService')
    def test_a_spent_override_is_not_taken_twice(self, tranzila_cls):
        tranzila_cls.production.return_value.charge_with_token.return_value = _ok()
        set_month_override(
            self.recurring,
            billing_month=self.this_month,
            amount=Decimal('150.00'),
            reason='חודש קצר',
            created_by=self.user,
        )
        process_due_recurring_charges()

        # Force a second pass in the same month, as a re-run of the cron would.
        RecurringPayment.objects.filter(pk=self.recurring.pk).update(
            next_billing_date=self.today, last_charge_date=None,
        )
        self.recurring.refresh_from_db()

        amount, override = amount_for_charge(self.recurring, on_date=self.today)
        self.assertEqual(amount, Decimal('275.00'))
        self.assertIsNone(override)

    @patch('apps.customers.recurring_billing.TranzilaService')
    def test_a_declined_charge_leaves_the_override_for_the_next_run(self, tranzila_cls):
        tranzila_cls.production.return_value.charge_with_token.return_value = {
            'success': False, 'error': 'declined', 'raw_response': {},
        }
        set_month_override(
            self.recurring,
            billing_month=self.this_month,
            amount=Decimal('150.00'),
            reason='חודש קצר',
            created_by=self.user,
        )

        process_due_recurring_charges()

        override = RecurringChargeOverride.objects.get(recurring_payment=self.recurring)
        self.assertIsNone(override.applied_at)

    # ---- what may be filed --------------------------------------------------

    def test_a_month_already_gone_is_refused(self):
        past = (self.this_month - timedelta(days=1)).replace(day=1)
        with self.assertRaises(ValueError):
            set_month_override(
                self.recurring,
                billing_month=past,
                amount=Decimal('150.00'),
                reason='מאוחר מדי',
            )

    def test_a_reason_is_required(self):
        with self.assertRaises(ValueError):
            set_month_override(
                self.recurring,
                billing_month=self.this_month,
                amount=Decimal('150.00'),
                reason='   ',
            )

    def test_an_inactive_standing_order_is_refused(self):
        self.recurring.status = 'cancelled'
        self.recurring.save(update_fields=['status'])
        with self.assertRaises(ValueError):
            set_month_override(
                self.recurring,
                billing_month=self.this_month,
                amount=Decimal('150.00'),
                reason='מבוטל',
            )

    def test_an_order_tranzila_bills_is_refused(self):
        # recurring_billing skips these, so an override on one would never be charged.
        self.recurring.tranzila_recurring_index = '4411'
        self.recurring.save(update_fields=['tranzila_recurring_index'])
        with self.assertRaises(ValueError):
            set_month_override(
                self.recurring,
                billing_month=self.this_month,
                amount=Decimal('150.00'),
                reason='מנוהל בטרנזילה',
            )

    def test_clearing_puts_the_month_back_to_the_regular_amount(self):
        set_month_override(
            self.recurring,
            billing_month=self.this_month,
            amount=Decimal('150.00'),
            reason='נקבע',
        )
        self.assertTrue(clear_month_override(self.recurring, billing_month=self.this_month))
        amount, override = amount_for_charge(self.recurring, on_date=self.today)
        self.assertEqual(amount, Decimal('275.00'))
        self.assertIsNone(override)

    # ---- the store rides on the same month ----------------------------------

    def test_store_purchases_add_up_rather_than_replace(self):
        add_to_month_override(
            self.recurring,
            extra=Decimal('120.00'),
            reason='רכישה בחנות: חולצה',
            billing_month=self.this_month,
        )
        add_to_month_override(
            self.recurring,
            extra=Decimal('80.00'),
            reason='רכישה בחנות: מכנסיים',
            billing_month=self.this_month,
        )

        amount, override = amount_for_charge(self.recurring, on_date=self.this_month)
        self.assertEqual(amount, Decimal('475.00'))
        self.assertEqual(override.source, 'store')
        self.assertIn('חולצה', override.reason)
        self.assertIn('מכנסיים', override.reason)

    def test_a_manual_change_and_a_store_purchase_stack_on_the_same_month(self):
        set_month_override(
            self.recurring,
            billing_month=self.this_month,
            amount=Decimal('150.00'),
            reason='חודש קצר',
        )
        add_to_month_override(
            self.recurring,
            extra=Decimal('120.00'),
            reason='רכישה בחנות: חולצה',
            billing_month=self.this_month,
        )
        amount, _ = amount_for_charge(self.recurring, on_date=self.this_month)
        self.assertEqual(amount, Decimal('270.00'))

    def test_the_till_part_is_tracked_apart_from_the_total(self):
        add_to_month_override(
            self.recurring,
            extra=Decimal('120.00'),
            reason='רכישה בחנות: חולצה',
            billing_month=self.this_month,
        )
        add_to_month_override(
            self.recurring,
            extra=Decimal('80.00'),
            reason='רכישה בחנות: מכנסיים',
            billing_month=self.this_month,
        )
        override = RecurringChargeOverride.objects.get(recurring_payment=self.recurring)
        self.assertEqual(override.amount, Decimal('475.00'))
        self.assertEqual(override.store_amount, Decimal('200.00'))

    def test_editing_a_month_by_hand_keeps_the_till_part(self):
        add_to_month_override(
            self.recurring,
            extra=Decimal('120.00'),
            reason='רכישה בחנות: חולצה',
            billing_month=self.this_month,
        )
        set_month_override(
            self.recurring,
            billing_month=self.this_month,
            amount=Decimal('300.00'),
            reason='תוקן ידנית',
        )
        override = RecurringChargeOverride.objects.get(recurring_payment=self.recurring)
        self.assertEqual(override.amount, Decimal('300.00'))
        self.assertEqual(override.store_amount, Decimal('120.00'))

    def test_the_till_part_never_exceeds_the_total(self):
        add_to_month_override(
            self.recurring,
            extra=Decimal('120.00'),
            reason='רכישה בחנות: חולצה',
            billing_month=self.this_month,
        )
        set_month_override(
            self.recurring,
            billing_month=self.this_month,
            amount=Decimal('50.00'),
            reason='הוזל',
        )
        override = RecurringChargeOverride.objects.get(recurring_payment=self.recurring)
        self.assertEqual(override.store_amount, Decimal('50.00'))

    @patch('apps.customers.recurring_billing.TranzilaService')
    def test_a_till_purchase_is_charged_as_two_lines(self, tranzila_cls):
        tranzila_cls.production.return_value.charge_with_token.return_value = _ok()
        add_to_month_override(
            self.recurring,
            extra=Decimal('120.00'),
            reason='רכישה בחנות: חולצה',
            billing_month=self.this_month,
        )

        process_due_recurring_charges()

        call = tranzila_cls.production.return_value.charge_with_token.call_args
        self.assertEqual(call.kwargs['amount'], Decimal('395.00'))
        items = call.kwargs['items']
        self.assertEqual(len(items), 2)
        self.assertEqual(sum(item['unit_price'] for item in items), 395.0)
        self.assertIn('רכישה בחנות', items[1]['name'])

        payment = Payment.objects.filter(payment_type='recurring_subscription').latest('created_at')
        self.assertIn('רכישה בחנות', payment.description)

    @patch('apps.customers.recurring_billing.TranzilaService')
    def test_a_manual_change_is_not_itemised_to_the_payer(self, tranzila_cls):
        tranzila_cls.production.return_value.charge_with_token.return_value = _ok()
        set_month_override(
            self.recurring,
            billing_month=self.this_month,
            amount=Decimal('150.00'),
            reason='סיבה פנימית שאסור שתגיע להורה',
        )

        process_due_recurring_charges()

        call = tranzila_cls.production.return_value.charge_with_token.call_args
        self.assertEqual(len(call.kwargs['items']), 1)
        payment = Payment.objects.filter(payment_type='recurring_subscription').latest('created_at')
        self.assertNotIn('סיבה פנימית', payment.description)
        self.assertNotIn('רכישה בחנות', payment.description)

    def test_only_one_row_is_kept_per_month(self):
        for extra in (Decimal('10.00'), Decimal('20.00'), Decimal('30.00')):
            add_to_month_override(
                self.recurring,
                extra=extra,
                reason='רכישה בחנות',
                billing_month=self.this_month,
            )
        self.assertEqual(
            RecurringChargeOverride.objects.filter(recurring_payment=self.recurring).count(),
            1,
        )


class StorePurchaseOnStandingOrderTests(TestCase):
    """A store sale told to ride on the standing order must actually reach it."""

    def setUp(self):
        self.today = timezone.localdate()
        self.this_month = self.today.replace(day=1)
        self.course = TestDataFactory.create_course(price=Decimal('275.00'))
        self.lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.family = TestDataFactory.create_family(phone='0500000222')
        self.child = TestDataFactory.create_child(family=self.family, first_name='נועם')

    def _standing_order(self):
        payment = Payment.objects.create(
            child=self.child,
            family=self.family,
            branch=self.course.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('275.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('275.00'),
        )
        return RecurringPayment.objects.create(
            child=self.child,
            initial_payment=payment,
            status='active',
            amount=Decimal('275.00'),
            base_amount=Decimal('275.00'),
            billing_day=1,
            start_date=self.this_month,
            next_billing_date=self.today,
            tranzila_token='tok-store',
            card_expire_month=12,
            card_expire_year=2030,
        )

    def _product(self, price):
        from apps.store.models import StoreProduct

        return StoreProduct.objects.create(
            name='חולצת קוגומלו',
            category='ביגוד',
            cost_price=Decimal('40.00'),
            sale_price=price,
            stock_quantity=10,
        )

    def test_a_purchase_lands_on_the_next_charged_month(self):
        from apps.core.payment_service import PaymentService

        recurring = self._standing_order()
        product = self._product(Decimal('120.00'))

        PaymentService().create_cash_invoice(
            product_items=[{'product_id': str(product.id), 'quantity': 1}],
            child_id=str(self.child.id),
            payment_method='monthly_billing',
        )

        override = RecurringChargeOverride.objects.get(recurring_payment=recurring)
        self.assertEqual(override.amount, Decimal('395.00'))
        self.assertEqual(override.source, 'store')
        self.assertIsNotNone(override.store_invoice)
        self.assertIn('חולצת קוגומלו', override.reason)

    def test_a_purchase_without_a_standing_order_is_refused(self):
        from apps.core.payment_service import PaymentService

        product = self._product(Decimal('120.00'))

        with self.assertRaises(ValueError):
            PaymentService().create_cash_invoice(
                product_items=[{'product_id': str(product.id), 'quantity': 1}],
                child_id=str(self.child.id),
                payment_method='monthly_billing',
            )

    def test_a_cash_purchase_does_not_touch_the_standing_order(self):
        from apps.core.payment_service import PaymentService

        recurring = self._standing_order()
        product = self._product(Decimal('120.00'))

        PaymentService().create_cash_invoice(
            product_items=[{'product_id': str(product.id), 'quantity': 1}],
            child_id=str(self.child.id),
            payment_method='cash',
        )

        self.assertFalse(
            RecurringChargeOverride.objects.filter(recurring_payment=recurring).exists()
        )
