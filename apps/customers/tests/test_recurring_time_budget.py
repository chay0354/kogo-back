"""The monthly run stops starting charges before Vercel can cut it.

On 1.10.2026 a charge with its signed receipt and its mail took about twenty
seconds, so every hourly run was killed at 300 seconds after some fifteen
standing orders. The 08:00 run was killed between the gateway call and its
answer: the payment stayed pending, the claim stayed open, and the standing
order was blocked until the office checked the terminal. The run also never
reached what follows the loop — the check and cash documents, the office alert,
the heartbeat.
"""
import json
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.customers.models import Payment, TranzilaTransaction
from apps.customers.recurring_billing import (
    CHARGE_BUDGET_SECONDS,
    DOCUMENTS_CUTOFF_SECONDS,
    process_due_recurring_charges,
)
from apps.customers.tests.test_charge_survives_receipt_failure import (
    TOKEN_CHARGE_OK,
    _due_standing_order,
    _today,
)

DOCUMENTS_DONE = {'checked': 0, 'issued': 0, 'errors': []}


class _Clock:
    """Seconds since the run began, moved by hand."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class RunStopsBeforeTheLimitTests(TestCase):
    def setUp(self):
        self.clock = _Clock()
        patcher = patch('apps.customers.recurring_billing._clock', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _charge_taking(self, seconds):
        def charge(*args, **kwargs):
            self.clock.now += seconds
            return dict(TOKEN_CHARGE_OK)
        return charge

    def _monthly_payments(self, recurring):
        return (
            Payment.objects
            .filter(child=recurring.child, payment_type='recurring_subscription')
            .exclude(pk=recurring.initial_payment_id)
        )

    def test_no_charge_starts_once_the_budget_is_spent(self):
        orders = [_due_standing_order() for _ in range(3)]
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   side_effect=self._charge_taking(CHARGE_BUDGET_SECONDS / 2)) as charge, \
                patch('apps.core.payment_service.PaymentService._create_invoice_from_payment'):
            summary = process_due_recurring_charges()

        self.assertEqual(charge.call_count, 2)
        self.assertEqual(summary['charged'], 2)
        self.assertTrue(summary['stopped_early'])
        self.assertEqual(summary['left_due'], 1)

        charged = [o for o in orders if self._monthly_payments(o).exists()]
        left = [o for o in orders if not self._monthly_payments(o).exists()]
        self.assertEqual(len(charged), 2)
        self.assertEqual(len(left), 1)
        # The one left was not touched: nothing claims its month, no payment
        # row waits on an answer, and it is still due exactly as before.
        untouched = left[0]
        untouched.refresh_from_db()
        self.assertEqual(untouched.status, 'active')
        self.assertEqual(untouched.next_billing_date, _today())
        self.assertIsNone(untouched.last_charge_date)
        self.assertFalse(
            TranzilaTransaction.objects
            .filter(idempotency_key__startswith=f'recurring_{untouched.id}_')
            .exists()
        )

    def test_the_next_run_charges_what_was_left_and_nobody_twice(self):
        orders = [_due_standing_order() for _ in range(3)]
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   side_effect=self._charge_taking(CHARGE_BUDGET_SECONDS / 2)) as charge, \
                patch('apps.core.payment_service.PaymentService._create_invoice_from_payment'):
            process_due_recurring_charges()
            self.clock.now = 0.0
            second = process_due_recurring_charges()

        self.assertEqual(charge.call_count, 3)
        self.assertEqual(second['charged'], 1)
        self.assertNotIn('stopped_early', second)
        for order in orders:
            self.assertEqual(self._monthly_payments(order).filter(status='completed').count(), 1)
            order.refresh_from_db()
            self.assertEqual(order.last_charge_date, _today())

    def test_a_run_inside_its_budget_reports_nothing_new(self):
        _due_standing_order()
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   side_effect=self._charge_taking(20)), \
                patch('apps.core.payment_service.PaymentService._create_invoice_from_payment'):
            summary = process_due_recurring_charges()
        self.assertEqual(summary['charged'], 1)
        self.assertNotIn('stopped_early', summary)
        self.assertNotIn('left_due', summary)

    def test_a_dry_run_counts_every_row_whatever_the_clock_says(self):
        for _ in range(3):
            _due_standing_order()
        self.clock.now = CHARGE_BUDGET_SECONDS + 100
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token') as charge:
            summary = process_due_recurring_charges(dry_run=True)
        charge.assert_not_called()
        self.assertEqual(summary['charged'], 3)
        self.assertNotIn('stopped_early', summary)

    def test_a_stopped_run_still_moves_one_check_and_one_cash_document(self):
        for _ in range(2):
            _due_standing_order()
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   side_effect=self._charge_taking(CHARGE_BUDGET_SECONDS)), \
                patch('apps.core.payment_service.PaymentService._create_invoice_from_payment'), \
                patch('apps.documents.check_plans.issue_due_check_invoices',
                      return_value=dict(DOCUMENTS_DONE)) as checks, \
                patch('apps.documents.cash_plans.issue_due_cash_documents',
                      return_value=dict(DOCUMENTS_DONE)) as cash:
            summary = process_due_recurring_charges()

        self.assertTrue(summary['stopped_early'])
        self.assertEqual(checks.call_args.kwargs['limit'], 1)
        self.assertEqual(cash.call_args.kwargs['limit'], 1)

    def test_a_finished_run_issues_documents_with_the_full_limit(self):
        _due_standing_order()
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   side_effect=self._charge_taking(20)), \
                patch('apps.core.payment_service.PaymentService._create_invoice_from_payment'), \
                patch('apps.documents.check_plans.issue_due_check_invoices',
                      return_value=dict(DOCUMENTS_DONE)) as checks, \
                patch('apps.documents.cash_plans.issue_due_cash_documents',
                      return_value=dict(DOCUMENTS_DONE)) as cash:
            process_due_recurring_charges(limit=40)

        self.assertEqual(checks.call_args.kwargs['limit'], 40)
        self.assertEqual(cash.call_args.kwargs['limit'], 40)

    def test_documents_wait_for_the_next_run_when_no_time_is_left(self):
        _due_standing_order()
        # The last charge ran long — a gateway timeout, a slow receipt.
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   side_effect=self._charge_taking(DOCUMENTS_CUTOFF_SECONDS + 1)), \
                patch('apps.core.payment_service.PaymentService._create_invoice_from_payment'), \
                patch('apps.documents.check_plans.issue_due_check_invoices') as checks, \
                patch('apps.documents.cash_plans.issue_due_cash_documents') as cash:
            summary = process_due_recurring_charges()

        checks.assert_not_called()
        cash.assert_not_called()
        self.assertTrue(summary['check_invoices']['deferred'])
        self.assertTrue(summary['cash_documents']['deferred'])
        self.assertEqual(summary['charged'], 1)


class ScheduleTests(TestCase):
    """The budget only holds if the runs it is split across are really scheduled."""

    def _billing_schedule(self):
        crons = json.loads((Path(settings.BASE_DIR) / 'vercel.json').read_text())['crons']
        return next(c['schedule'] for c in crons if c['path'].endswith('/customers/cron/recurring-billing/'))

    def test_the_budget_leaves_room_under_the_function_limit(self):
        # A row that starts at the last allowed second may still wait 30 seconds
        # on the gateway and then issue its receipt.
        self.assertLessEqual(CHARGE_BUDGET_SECONDS + 60, 300)
        self.assertLess(CHARGE_BUDGET_SECONDS, DOCUMENTS_CUTOFF_SECONDS)
        self.assertLess(DOCUMENTS_CUTOFF_SECONDS, 300)

    def test_runs_are_five_minutes_apart_the_longest_a_run_can_live(self):
        # The gap equals the function limit, so a run is over — finished or
        # killed — by the time the next one starts. Should two ever meet, the
        # unique claim lets only one of them charge a standing order.
        minute, hours, *rest = self._billing_schedule().split()
        self.assertEqual(minute, '*/5')
        self.assertEqual(rest, ['*', '*', '*'])
        first, last = (int(h) for h in hours.split('-'))
        # 08:00 to 20:55 on Israel's summer clock, 07:00 to 19:55 in winter.
        self.assertEqual((first, last), (5, 17))

    @override_settings(CRON_TOKEN='test-cron-token')
    def test_the_status_endpoint_reports_the_schedule_that_is_deployed(self):
        res = APIClient().get(
            '/api/v1/customers/cron/recurring-billing/status/',
            HTTP_X_CRON_TOKEN='test-cron-token',
        )
        self.assertEqual(res.json()['schedule_utc'], self._billing_schedule())
