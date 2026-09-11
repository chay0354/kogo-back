"""The monthly run: the reserved-row guard, review on an unknown answer, failed on a decline,
the receipt after the charge, and the schedule (billing day, end date, pause)."""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import Business
from apps.rental_billing.billing import charge_due, issue_receipt_safely, split_amount
from apps.rental_billing.models import TenantCardLink, TenantCharge, TenantStandingOrder
from apps.rental_billing.orders import pause_order, resume_order, update_standing_order
from apps.rental_billing.schedule import add_months, first_of_month
from apps.rental_billing.tests.factories import (
    DECLINE, NET_AGOROT, OK_TOKEN_CHARGE, TIMEOUT, TOTAL_AGOROT, VAT_AGOROT, BillingFixture,
)

OCT = date(2026, 10, 1)
Order = TenantStandingOrder
Charge = TenantCharge


@override_settings(RENTAL_BILLING_ENABLED=True)
class CronChargingTests(BillingFixture, TestCase):
    def test_vat_is_split_in_agorot_at_the_current_rate(self):
        self.assertEqual(split_amount(Decimal('1234.56')), (NET_AGOROT, VAT_AGOROT, TOTAL_AGOROT))
        self.assertEqual(split_amount(Decimal('1.00')), (100, 18, 118))

    def test_a_due_order_is_charged_once_and_gets_its_receipt(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))

        summary = charge_due(today=date(2026, 10, 10))

        self.assertEqual((summary['charged'], summary['receipts'], summary['failed']), (1, 1, 0), summary)
        charge = Charge.objects.get()
        self.assertEqual(charge.period, OCT)
        self.assertEqual((charge.amount_before_vat, charge.vat_amount, charge.total), (NET_AGOROT, VAT_AGOROT, TOTAL_AGOROT))
        self.assertEqual(charge.status, Charge.STATUS_CHARGED)
        self.assertEqual((charge.transaction_id, charge.confirmation_code), ('T100', 'C100'))
        self.assertEqual(charge.business, self.business)
        self.assertEqual(charge.card_last4, '4242')
        self.assertTrue(charge.receipt.document_number.startswith('RT-'))
        kwargs = self.gateway.charge_with_token.call_args.kwargs
        self.assertEqual(kwargs['amount'], Decimal('1456.78'))
        self.assertEqual(kwargs['token'], 'tok_saved')
        self.assertEqual((kwargs['expire_month'], kwargs['expire_year']), (12, 2030))
        self.assertEqual(kwargs['duplicate_guard_key'], f'rental-{order.pk}-2026-10')
        order.refresh_from_db()
        self.assertEqual(order.next_charge_date, date(2026, 11, 10))
        self.assertEqual(order.status, Order.STATUS_ACTIVE)

    def test_nothing_is_due_before_the_billing_day(self):
        self.active_order(next_charge_date=date(2026, 10, 10))
        summary = charge_due(today=date(2026, 10, 9))
        self.assertEqual(summary['checked'], 0)
        self.assertEqual(self.gateway_calls(), 0)

    def test_a_second_run_does_not_charge_again(self):
        self.active_order(next_charge_date=date(2026, 10, 10))
        charge_due(today=date(2026, 10, 10))
        charge_due(today=date(2026, 10, 10))
        charge_due(today=date(2026, 10, 25))
        self.assertEqual(self.gateway.charge_with_token.call_count, 1)
        self.assertEqual(Charge.objects.count(), 1)

    def test_a_charged_month_is_not_sent_again_when_the_schedule_did_not_move(self):
        # A crash after the charge was recorded but before the schedule moved:
        # the month's row stops the next run, and the schedule catches up.
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        self.charge_row(order, OCT, Charge.STATUS_CHARGED, charged_at=timezone.now())
        summary = charge_due(today=date(2026, 10, 10))
        self.assertEqual(self.gateway_calls(), 0)
        self.assertEqual(summary['skipped'], 1)
        order.refresh_from_db()
        self.assertEqual(order.next_charge_date, date(2026, 11, 10))

    def test_a_reserved_month_is_never_sent_again(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        reserved = self.charge_row(order, OCT, Charge.STATUS_RESERVED)
        charge_due(today=date(2026, 10, 10))
        charge_due(today=date(2026, 10, 11))
        self.assertEqual(self.gateway_calls(), 0)
        reserved.refresh_from_db()
        self.assertEqual(reserved.status, Charge.STATUS_RESERVED)
        order.refresh_from_db()
        self.assertEqual(order.next_charge_date, date(2026, 10, 10))

    def test_a_timeout_goes_to_review_and_is_never_retried(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        self.gateway.charge_with_token.return_value = dict(TIMEOUT)

        summary = charge_due(today=date(2026, 10, 10))

        self.assertEqual(summary['review'], 1)
        charge = Charge.objects.get()
        self.assertEqual(charge.status, Charge.STATUS_REVIEW)
        self.assertIsNone(charge.receipt_id)
        order.refresh_from_db()
        # Not a decline: the order is not failed and the tenant is sent no link.
        self.assertEqual(order.status, Order.STATUS_ACTIVE)
        self.assertFalse(TenantCardLink.objects.exists())

        self.gateway.charge_with_token.reset_mock()
        self.gateway.charge_with_token.return_value = dict(OK_TOKEN_CHARGE)
        for day in (date(2026, 10, 11), date(2026, 10, 30), date(2026, 11, 1)):
            charge_due(today=day)
        self.gateway.charge_with_token.assert_not_called()
        charge.refresh_from_db()
        self.assertEqual(charge.status, Charge.STATUS_REVIEW)

    def test_an_exception_from_the_gateway_is_a_review_not_a_decline(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        self.gateway.charge_with_token.side_effect = ConnectionError('connection reset')
        charge_due(today=date(2026, 10, 10))
        self.assertEqual(Charge.objects.get().status, Charge.STATUS_REVIEW)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_ACTIVE)

    def test_a_stale_reservation_goes_to_review(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        stale = self.charge_row(
            order, date(2026, 9, 1), Charge.STATUS_RESERVED, reserved_at=timezone.now() - timedelta(minutes=20),
        )
        fresh = self.charge_row(order, date(2026, 8, 1), Charge.STATUS_RESERVED)
        summary = charge_due(today=date(2026, 10, 9))
        self.assertEqual(summary['stale_to_review'], 1)
        stale.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(stale.status, Charge.STATUS_REVIEW)
        self.assertEqual(fresh.status, Charge.STATUS_RESERVED)

    def test_a_decline_fails_the_order_and_opens_a_card_link(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        self.gateway.charge_with_token.return_value = dict(DECLINE)

        summary = charge_due(today=date(2026, 10, 10))

        self.assertEqual(summary['failed'], 1)
        charge = Charge.objects.get()
        self.assertEqual(charge.status, Charge.STATUS_FAILED)
        self.assertEqual(charge.response_code, '033')
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_FAILED)
        self.assertIn('declined', order.last_error)
        self.assertIsNotNone(order.failed_at)
        self.assertEqual(TenantCardLink.objects.filter(standing_order=order, status=TenantCardLink.STATUS_PENDING).count(), 1)

        # A failed order is not charged again by the run; the office or the tenant's new card does it.
        self.gateway.charge_with_token.reset_mock()
        charge_due(today=date(2026, 10, 11))
        self.gateway.charge_with_token.assert_not_called()

    def test_a_receipt_that_fails_leaves_the_charge_charged(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        with patch('apps.rental_billing.receipts.next_document_number', side_effect=RuntimeError('series locked')):
            summary = charge_due(today=date(2026, 10, 10))

        self.assertEqual((summary['charged'], summary['receipts']), (1, 0))
        charge = Charge.objects.get()
        self.assertEqual(charge.status, Charge.STATUS_CHARGED)
        self.assertIsNone(charge.receipt_id)
        self.assertIn('series locked', charge.receipt_error)
        order.refresh_from_db()
        self.assertEqual(order.next_charge_date, date(2026, 11, 10))

        # The office issues it later, once.
        self.assertTrue(issue_receipt_safely(charge.pk))
        self.assertTrue(issue_receipt_safely(charge.pk))
        charge.refresh_from_db()
        self.assertTrue(charge.receipt.document_number.startswith('RT-'))
        self.assertEqual(charge.receipt_error, '')
        self.assertEqual(self.gateway_calls(), 1)

    def test_the_order_ends_once_the_next_charge_passes_its_end_date(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10), end_date=date(2026, 11, 5))
        charge_due(today=date(2026, 10, 10))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_ENDED)
        self.assertEqual(Charge.objects.get().status, Charge.STATUS_CHARGED)

    def test_an_order_past_its_end_date_is_ended_without_a_charge(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10), end_date=date(2026, 10, 5))
        summary = charge_due(today=date(2026, 10, 10))
        self.assertEqual(summary['ended'], 1)
        self.assertEqual(self.gateway_calls(), 0)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_ENDED)

    def test_a_paused_order_is_not_charged_and_resumes_without_the_paused_months(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        pause_order(order)
        charge_due(today=date(2026, 10, 10))
        charge_due(today=date(2026, 11, 10))
        self.assertEqual(self.gateway_calls(), 0)

        order = resume_order(order, today=date(2026, 12, 3))
        self.assertEqual(order.status, Order.STATUS_ACTIVE)
        self.assertEqual(order.next_charge_date, date(2026, 12, 10))

    def test_resuming_before_the_billing_day_keeps_the_month(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        pause_order(order)
        order = resume_order(order, today=date(2026, 10, 5))
        self.assertEqual(order.next_charge_date, date(2026, 10, 10))

    def test_resuming_skips_a_month_that_is_already_charged(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        self.charge_row(order, OCT, Charge.STATUS_CHARGED, charged_at=timezone.now())
        pause_order(order)
        order = resume_order(order, today=date(2026, 10, 5))
        self.assertEqual(order.next_charge_date, date(2026, 11, 10))

    def test_changing_the_billing_day_moves_the_next_charge_within_its_month(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        order = update_standing_order(order, {'billing_day': 20})
        self.assertEqual(order.next_charge_date, date(2026, 10, 20))
        self.assertEqual(charge_due(today=date(2026, 10, 10))['checked'], 0)
        self.assertEqual(charge_due(today=date(2026, 10, 20))['charged'], 1)

    def test_months_behind_are_caught_up_one_a_day(self):
        today = timezone.localdate()
        behind = add_months(first_of_month(today), -2).replace(day=10)
        order = self.active_order(next_charge_date=behind)
        charge_due(today=today)
        charge_due(today=today)
        self.assertEqual(self.gateway.charge_with_token.call_count, 1)
        order.refresh_from_db()
        self.assertEqual(order.next_charge_date, add_months(first_of_month(behind), 1).replace(day=10))

    def test_a_missing_business_refuses_the_whole_run(self):
        self.active_order(next_charge_date=date(2026, 10, 10))
        Business.objects.filter(pk=self.business.pk).update(name='עסק אחר')
        summary = charge_due(today=date(2026, 10, 10))
        self.assertFalse(summary['ok'])
        self.assertIn('סוחרים', summary['error'])
        self.assertEqual(self.gateway_calls(), 0)
        self.assertFalse(Charge.objects.exists())
        self.assertFalse(Business.objects.filter(name='סוחרים').exists())

    def test_an_unconfigured_terminal_refuses_the_whole_run(self):
        self.active_order(next_charge_date=date(2026, 10, 10))
        self.gateway.credential_error.return_value = 'TRANZILA_TERMINAL not configured'
        summary = charge_due(today=date(2026, 10, 10))
        self.assertFalse(summary['ok'])
        self.assertEqual(self.gateway_calls(), 0)
        self.assertFalse(Charge.objects.exists())
