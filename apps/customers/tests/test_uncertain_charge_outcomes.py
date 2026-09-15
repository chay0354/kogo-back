"""An answer that never came back from Tranzila is not a decline.

`charge_with_token` / `charge_with_card` hand back `uncertain=True` on a
timeout or a dropped connection: the card may already have been charged.
Every path that then treated the answer as "declined" opened a second charge
for the same money — the monthly run an hour later, the office pressing
"charge" again, a parent trying the card-update link a second time.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.core.payment_service import PaymentService
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.card_update import CardUpdateError, apply_new_card, resolve_card_update_intent
from apps.customers.discount_service import DiscountCalculation
from apps.customers.models import Payment, TranzilaTransaction
from apps.customers.recurring_billing import process_due_recurring_charges
from apps.customers.tests.test_card_update import CARD
from apps.customers.tests.test_card_update_modes import RenewChargeTests, make_sto, month_start
from apps.customers.tests.test_charge_survives_receipt_failure import (
    TOKEN_CHARGE_OK,
    _due_standing_order,
    _today,
)
from datetime import timedelta

UNCERTAIN = {
    'success': False,
    'uncertain': True,
    'error': 'Charge failed - exception: Read timed out',
    'message': 'Charge failed - exception',
}


def _passthrough_discount(**kwargs):
    return DiscountCalculation(
        applicable_discounts=[],
        total_discount_amount=Decimal('0.00'),
        final_price=kwargs['base_price'],
        base_price=kwargs['base_price'],
    )


class MonthlyRunUncertainTest(TestCase):
    def _monthly_payment(self, recurring):
        return (
            Payment.objects
            .filter(child=recurring.child, payment_type='recurring_subscription')
            .exclude(pk=recurring.initial_payment_id)
            .get()
        )

    def test_a_timeout_is_not_a_decline_and_the_next_run_does_not_charge_again(self):
        recurring = _due_standing_order()
        child = recurring.child
        status_before = child.status
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   return_value=dict(UNCERTAIN)) as charge, \
                patch('apps.customers.card_update.send_card_update_whatsapp') as whatsapp:
            first = process_due_recurring_charges()
            second = process_due_recurring_charges()

        # One call to the gateway, whatever the number of runs.
        self.assertEqual(charge.call_count, 1)
        self.assertEqual(first['charged'], 0)
        self.assertEqual(second['charged'], 0)
        self.assertTrue(any(str(recurring.id) in err for err in second['errors']))
        # Nobody is told their card was declined for an answer that never arrived.
        whatsapp.assert_not_called()
        recurring.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(recurring.status, 'active')
        self.assertEqual(recurring.next_billing_date, _today())
        self.assertIsNone(recurring.last_charge_date)
        self.assertEqual(child.status, status_before)
        monthly = self._monthly_payment(recurring)
        self.assertEqual(monthly.status, 'processing')
        self.assertIn('timed out', monthly.failure_reason)

    def test_a_decline_still_takes_the_failure_path(self):
        recurring = _due_standing_order()
        declined = {'success': False, 'error': 'הכרטיס נדחה', 'response_code': '004'}
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   return_value=declined), \
                patch('apps.customers.card_update.send_card_update_whatsapp') as whatsapp:
            summary = process_due_recurring_charges()
        self.assertEqual(summary['failed'], 1)
        whatsapp.assert_called_once()
        recurring.refresh_from_db()
        self.assertEqual(recurring.status, 'failed')
        self.assertEqual(recurring.child.status, 'payment_problem')
        self.assertEqual(self._monthly_payment(recurring).status, 'failed')
        # A decline took no money: nothing claims the month.
        self.assertFalse(
            TranzilaTransaction.objects.filter(idempotency_key__startswith=f'recurring_{recurring.id}_').exists()
        )

    def test_a_crash_after_the_charge_is_recorded_and_never_charged_again(self):
        recurring = _due_standing_order()
        # The database drops out right after Tranzila said yes, while the child's
        # paid-until date is being written.
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   return_value=dict(TOKEN_CHARGE_OK)) as charge, \
                patch('apps.customers.recurring_billing._paid_until',
                      side_effect=RuntimeError('server closed the connection unexpectedly')):
            first = process_due_recurring_charges()
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   return_value=dict(TOKEN_CHARGE_OK)) as charge_again:
            second = process_due_recurring_charges()

        self.assertEqual(charge.call_count, 1)
        charge_again.assert_not_called()
        self.assertEqual(second['charged'], 0)
        self.assertTrue(first['errors'])
        monthly = self._monthly_payment(recurring)
        # The charge itself is on record even though the run did not finish.
        self.assertEqual(monthly.status, 'completed')
        self.assertEqual(monthly.tranzila_transaction.transaction_id, 'TRX_MONTH')
        recurring.refresh_from_db()
        self.assertEqual(recurring.last_charge_date, _today())
        self.assertGreater(recurring.next_billing_date, _today())

    def test_a_crash_before_the_charge_is_recorded_blocks_the_next_run(self):
        recurring = _due_standing_order()
        # Even the smallest write after the gateway can fail; the claim taken
        # before the charge is what stops the next run from paying twice.
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   return_value=dict(TOKEN_CHARGE_OK)) as charge, \
                patch('apps.customers.recurring_billing._next_month_first',
                      side_effect=RuntimeError('server closed the connection unexpectedly')):
            process_due_recurring_charges()
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   return_value=dict(TOKEN_CHARGE_OK)) as charge_again:
            second = process_due_recurring_charges()

        self.assertEqual(charge.call_count, 1)
        charge_again.assert_not_called()
        self.assertEqual(second['charged'], 0)
        self.assertTrue(any(str(recurring.id) in err for err in second['errors']))
        recurring.refresh_from_db()
        self.assertEqual(recurring.status, 'active')

    def test_a_successful_charge_leaves_exactly_one_transaction_row(self):
        recurring = _due_standing_order()
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   return_value=dict(TOKEN_CHARGE_OK)), \
                patch('apps.core.payment_service.PaymentService._create_invoice_from_payment'):
            summary = process_due_recurring_charges()
        self.assertEqual(summary['charged'], 1)
        rows = TranzilaTransaction.objects.filter(idempotency_key=f'recurring_{recurring.id}_{_today().isoformat()}')
        self.assertEqual(rows.count(), 1)
        self.assertTrue(rows.get().is_successful)
        self.assertEqual(rows.get().transaction_id, 'TRX_MONTH')
        self.assertEqual(self._monthly_payment(recurring).tranzila_transaction_id, rows.get().id)


class CardUpdateRenewUncertainTest(TestCase):
    def test_a_timeout_keeps_the_claim_so_the_link_cannot_pay_twice(self):
        recurring = make_sto(
            amount='250.00', next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        token, _ = RenewChargeTests._renew_token(self, recurring)
        with patch('apps.core.tranzila_service.TranzilaService.charge_with_card',
                   return_value=dict(UNCERTAIN)) as charge:
            with self.assertRaises(CardUpdateError) as first:
                apply_new_card(recurring, CARD, intent=resolve_card_update_intent(token))
            with self.assertRaises(CardUpdateError):
                apply_new_card(recurring, CARD, intent=resolve_card_update_intent(token))
        self.assertEqual(charge.call_count, 1)
        self.assertIn('משרד', str(first.exception))
        claims = TranzilaTransaction.objects.filter(idempotency_key__startswith='card_update_renew_')
        self.assertEqual(claims.count(), 1)
        self.assertFalse(claims.get().is_successful)
        payment = Payment.objects.filter(child=recurring.child).exclude(pk=recurring.initial_payment_id).get()
        self.assertEqual(payment.status, 'processing')
        recurring.refresh_from_db()
        self.assertEqual(recurring.tranzila_token, 'oldtoken')
        self.assertEqual(recurring.next_billing_date, month_start(-1))


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class CrmChargeUncertainTest(TestCase):
    def _charge(self, service, child, lesson):
        return service.charge_subscription_with_card(
            child_id=str(child.id),
            lesson_id=str(lesson.id),
            card_number='4580458045804580',
            expiry_month=12,
            expiry_year=2030,
            cvv='123',
            card_holder_id='123456782',
        )

    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment', side_effect=_passthrough_discount)
    @patch('apps.core.payment_service.TranzilaService.charge_with_card', return_value=dict(UNCERTAIN))
    def test_a_timeout_leaves_the_charge_in_processing_and_blocks_the_office_from_charging_again(self, charge, _discount):
        child = TestDataFactory.create_child()
        lesson = TestDataFactory.create_lesson()
        status_before = child.status
        service = PaymentService()

        result = self._charge(service, child, lesson)
        self.assertFalse(result['success'])
        self.assertIn('אל תחייבו שוב', result['error'])
        payment = Payment.objects.get(child=child, lesson=lesson)
        self.assertEqual(payment.status, 'processing')
        child.refresh_from_db()
        self.assertEqual(child.status, status_before)

        with self.assertRaises(ValueError) as blocked:
            self._charge(service, child, lesson)
        self.assertIn('בבדיקה', str(blocked.exception))
        self.assertEqual(charge.call_count, 1)
        self.assertEqual(Payment.objects.filter(child=child, lesson=lesson).count(), 1)

    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment', side_effect=_passthrough_discount)
    @patch('apps.core.payment_service.TranzilaService.charge_with_card',
           return_value={'success': False, 'error': 'הכרטיס נדחה', 'response_code': '004'})
    def test_a_decline_still_fails_the_payment_and_allows_another_card(self, charge, _discount):
        child = TestDataFactory.create_child()
        lesson = TestDataFactory.create_lesson()
        service = PaymentService()
        self.assertFalse(self._charge(service, child, lesson)['success'])
        self.assertEqual(Payment.objects.get(child=child, lesson=lesson).status, 'failed')
        self.assertFalse(self._charge(service, child, lesson)['success'])
        self.assertEqual(charge.call_count, 2)
