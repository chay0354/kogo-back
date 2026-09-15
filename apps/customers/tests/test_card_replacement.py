"""Family-wide card replacement: targets, arrears, the swap, and the money."""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.core.card_validation import CardValidationError
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.card_replacement import (
    CardReplacementError,
    build_targets,
    family_standing_orders,
    months_outstanding,
    quote,
    replace_card,
)
from apps.customers.models import (
    CardReplacement,
    Payment,
    RecurringChargeOverride,
    RecurringPayment,
    TranzilaTransaction,
)

TODAY = date(2026, 11, 15)

CARD = {
    'card_number': '4580458045804580',
    'expiry_month': 12,
    'expiry_year': 2030,
    'cvv': '123',
    'card_holder_id': '123456782',
}
DINERS = {**CARD, 'card_number': '30569309025904'}

VERIFY_OK = {
    'success': True,
    'token': 'Ynewtoken4580',
    'response_code': '000',
    'raw_response': {'transaction_result': {'token': 'Ynewtoken4580'}},
}
VERIFY_NO_TOKEN = {'success': True, 'token': '', 'response_code': '000', 'raw_response': {}}
CHARGE_OK = {
    'success': True,
    'transaction_id': 'cr-1',
    'confirmation_code': 'AUTH',
    'response_code': '000',
    'raw_response': {},
}
CHARGE_DECLINED = {'success': False, 'error': 'אין כיסוי מספיק', 'response_code': '004'}


def _sto(family, *, child=None, amount='240.00', status='failed', token='OLDTOKEN',
         next_billing=date(2026, 9, 1), lesson=None):
    child = child or TestDataFactory.create_child(family=family)
    lesson = lesson or TestDataFactory.create_lesson()
    initial = Payment.objects.create(
        child=child, family=family, parent=family.parents.first(),
        lesson=lesson, branch=lesson.course.branch,
        payment_type='recurring_subscription', status='completed',
        base_amount=Decimal(amount), discount_amount=Decimal('0.00'),
        final_amount=Decimal(amount), registration_fee=Decimal('0.00'),
        description='מנוי',
    )
    return RecurringPayment.objects.create(
        child=child, initial_payment=initial, tranzila_token=token, status=status,
        base_amount=Decimal(amount), amount=Decimal(amount), billing_day=1,
        start_date=date(2026, 8, 1), next_billing_date=next_billing,
        card_expire_month=8, card_expire_year=2026,
    )


def _family():
    family = TestDataFactory.create_family()
    TestDataFactory.create_parent(family=family)
    return family


class ArrearsTests(TestCase):
    def test_months_from_next_billing_date_up_to_today(self):
        rec = _sto(_family(), next_billing=date(2026, 9, 1))
        due = months_outstanding(rec, today=TODAY)
        self.assertEqual([d.month for d in due],
                         [date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)])
        self.assertEqual(sum(d.amount for d in due), Decimal('720.00'))

    def test_a_month_already_paid_is_dropped(self):
        rec = _sto(_family(), next_billing=date(2026, 9, 1))
        Payment.objects.create(
            child=rec.child, family=rec.child.family, lesson=rec.initial_payment.lesson,
            branch=rec.initial_payment.lesson.course.branch,
            payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('240'), final_amount=Decimal('240'),
            registration_fee=Decimal('0.00'), payment_date=date(2026, 10, 12),
            description='כבר שולם',
        )
        self.assertEqual([d.month for d in months_outstanding(rec, today=TODAY)],
                         [date(2026, 9, 1), date(2026, 11, 1)])

    def test_a_month_override_is_respected(self):
        """The bug in the one-order link: it bills the plain monthly figure."""
        rec = _sto(_family(), next_billing=date(2026, 10, 1))
        RecurringChargeOverride.objects.create(
            recurring_payment=rec, billing_month=date(2026, 10, 1),
            amount=Decimal('310.00'), store_amount=Decimal('70.00'), reason='חולצה',
        )
        due = months_outstanding(rec, today=TODAY)
        self.assertEqual([d.amount for d in due], [Decimal('310.00'), Decimal('240.00')])

    def test_nothing_owed_when_next_billing_is_in_the_future(self):
        rec = _sto(_family(), next_billing=date(2026, 12, 1))
        self.assertEqual(months_outstanding(rec, today=TODAY), [])

    def test_arrears_are_capped(self):
        rec = _sto(_family(), next_billing=date(2020, 1, 1))
        self.assertLessEqual(len(months_outstanding(rec, today=TODAY)), 12)


class TargetTests(TestCase):
    def test_every_child_and_course_in_the_family(self):
        family = _family()
        _sto(family, next_billing=date(2026, 11, 1))
        _sto(family, next_billing=date(2026, 11, 1), status='active')
        self.assertEqual(len(family_standing_orders(family)), 2)

    def test_cancelled_is_left_alone(self):
        family = _family()
        _sto(family, status='cancelled')
        self.assertEqual(family_standing_orders(family), [])

    def test_tranzila_managed_order_is_flagged_not_touched(self):
        family = _family()
        rec = _sto(family, status='active')
        rec.tranzila_recurring_index = 'STO-9'
        rec.save(update_fields=['tranzila_recurring_index'])
        target = build_targets(family, today=TODAY)[0]
        self.assertTrue(target.skip_reason)
        self.assertFalse(target.as_dict()['will_update_card'])

    def test_quote_totals_only_what_it_will_charge(self):
        family = _family()
        _sto(family, next_billing=date(2026, 10, 1))
        _sto(family, next_billing=date(2026, 11, 1), amount='100.00')
        result = quote(family, today=TODAY)
        self.assertEqual(result['standing_orders'], 2)
        self.assertEqual(result['total_due'], '580.00')
        self.assertTrue(result['will_charge'])


@patch('apps.core.payment_service.PaymentService._create_invoice_from_payment')
class ReplaceTests(TestCase):
    def test_one_card_repoints_every_order_and_collects_arrears(self, _inv):
        family = _family()
        a = _sto(family, next_billing=date(2026, 10, 1))
        b = _sto(family, next_billing=date(2026, 11, 1), amount='100.00', status='active')
        with patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=VERIFY_OK) as ver, \
             patch('apps.core.tranzila_service.TranzilaService.charge_with_token', return_value=CHARGE_OK) as chg:
            out = replace_card(family, CARD, source='crm', today=TODAY)

        self.assertEqual(ver.call_count, 1, 'הכרטיס נבדק פעם אחת בלבד')
        self.assertEqual(chg.call_count, 3, 'שני חודשים לילד א ואחד לילד ב')
        self.assertEqual(out['standing_orders_updated'], 2)
        self.assertEqual(out['charged_total'], '580.00')

        for rec in (a, b):
            rec.refresh_from_db()
            self.assertEqual(rec.tranzila_token, 'Ynewtoken4580')
            self.assertEqual(rec.status, 'active')
            self.assertEqual(rec.card_expire_year, 2030)
            self.assertEqual(rec.next_billing_date, date(2026, 12, 1))

    def test_a_decline_does_not_take_the_card_away(self, _inv):
        family = _family()
        rec = _sto(family, next_billing=date(2026, 11, 1))
        with patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=VERIFY_OK), \
             patch('apps.core.tranzila_service.TranzilaService.charge_with_token', return_value=CHARGE_DECLINED):
            out = replace_card(family, CARD, today=TODAY)

        rec.refresh_from_db()
        self.assertEqual(rec.tranzila_token, 'Ynewtoken4580')
        self.assertEqual(rec.status, 'active', 'ההוראה חיה — החודש הבא ייגבה מעצמו')
        self.assertEqual(out['charged_total'], '0.00')
        self.assertEqual(out['results'][0]['status'], 'declined')

    def test_running_twice_does_not_charge_the_same_month_twice(self, _inv):
        family = _family()
        _sto(family, next_billing=date(2026, 11, 1))
        with patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=VERIFY_OK), \
             patch('apps.core.tranzila_service.TranzilaService.charge_with_token', return_value=CHARGE_OK) as chg:
            replace_card(family, CARD, today=TODAY)
            self.assertEqual(chg.call_count, 1)
            replace_card(family, CARD, today=TODAY)
        self.assertEqual(chg.call_count, 1, 'החודש כבר נגבה — לא נשלח שוב')

    def test_a_card_with_no_token_is_refused_before_any_money_moves(self, _inv):
        """The Diners shape: the gateway is happy and hands back nothing to bill with."""
        family = _family()
        rec = _sto(family, next_billing=date(2026, 11, 1))
        with patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=VERIFY_NO_TOKEN), \
             patch('apps.core.tranzila_service.TranzilaService.charge_with_token') as chg:
            with self.assertRaises(CardReplacementError) as ctx:
                replace_card(family, CARD, today=TODAY)
        self.assertIn('טוקן', str(ctx.exception))
        chg.assert_not_called()
        rec.refresh_from_db()
        self.assertEqual(rec.tranzila_token, 'OLDTOKEN', 'הכרטיס הישן לא נדרס בטוקן ריק')

    def test_a_diners_card_never_reaches_the_gateway(self, _inv):
        family = _family()
        _sto(family, next_billing=date(2026, 11, 1))
        with patch('apps.core.tranzila_service.TranzilaService.verify_card') as ver:
            with self.assertRaises(CardValidationError) as ctx:
                replace_card(family, DINERS, today=TODAY)
        self.assertIn('דיינרס', str(ctx.exception))
        ver.assert_not_called()

    def test_a_family_with_nothing_to_update_says_so(self, _inv):
        with self.assertRaises(CardReplacementError):
            replace_card(_family(), CARD, today=TODAY)

    def test_the_swap_is_recorded_without_the_card_number(self, _inv):
        family = _family()
        _sto(family, next_billing=date(2026, 11, 1))
        with patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=VERIFY_OK), \
             patch('apps.core.tranzila_service.TranzilaService.charge_with_token', return_value=CHARGE_OK):
            replace_card(family, CARD, source='parent_link', today=TODAY)

        row = CardReplacement.objects.get(family=family)
        self.assertEqual(row.card_last4, '4580')
        self.assertEqual(row.card_brand, 'visa')
        self.assertEqual(row.source, 'parent_link')
        self.assertEqual(row.charged_amount, Decimal('240.00'))
        self.assertNotIn('4580458045804580', str(row.__dict__))

    def test_the_gateway_record_is_written_even_if_bookkeeping_fails(self, invoice):
        """docs/12 finding A: the money moved, so the evidence must survive."""
        invoice.side_effect = RuntimeError('חשבונית נכשלה')
        family = _family()
        _sto(family, next_billing=date(2026, 11, 1))
        with patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=VERIFY_OK), \
             patch('apps.core.tranzila_service.TranzilaService.charge_with_token', return_value=CHARGE_OK):
            out = replace_card(family, CARD, today=TODAY)

        self.assertEqual(out['results'][0]['status'], 'charged_with_errors')
        self.assertTrue(TranzilaTransaction.objects.filter(is_successful=True).exists())

    def test_an_uncertain_answer_is_not_a_decline(self, _inv):
        family = _family()
        _sto(family, next_billing=date(2026, 11, 1))
        uncertain = {'success': False, 'error': 'timeout', 'uncertain': True, 'response_code': '999'}
        with patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=VERIFY_OK), \
             patch('apps.core.tranzila_service.TranzilaService.charge_with_token', return_value=uncertain):
            out = replace_card(family, CARD, today=TODAY)
        self.assertEqual(out['results'][0]['status'], 'uncertain')
        self.assertEqual(Payment.objects.filter(status='processing').count(), 1)

    def test_reminder_counters_reset_on_a_successful_swap(self, _inv):
        family = _family()
        rec = _sto(family, next_billing=date(2026, 11, 1))
        RecurringPayment.objects.filter(id=rec.id).update(card_update_reminders_sent=3)
        with patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=VERIFY_OK), \
             patch('apps.core.tranzila_service.TranzilaService.charge_with_token', return_value=CHARGE_OK):
            replace_card(family, CARD, today=TODAY)
        rec.refresh_from_db()
        self.assertEqual(rec.card_update_reminders_sent, 0)
