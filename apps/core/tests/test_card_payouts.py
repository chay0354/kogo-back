"""
Money about to come in (apps/core/card_payouts.py): the rule of the 6th, the
reading of Tranzila's report, the card money the CRM recorded — by source and
by branch, each shekel once — the dashboard endpoints' permissions, and the
brief's item.

Tranzila is never reached: the report is a mock, and the real client's request
method fails the test if anything gets to it.
"""
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.card_payouts import (
    PayoutError,
    classify_report_row,
    our_card_money,
    payout_for_month,
    payout_terminals,
    refresh_terminal_month,
    summarise_report_rows,
    upcoming_payout,
)
from apps.core.models import Business, CardPayoutTerminalMonth, UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Payment, TranzilaTransaction
from apps.documents.models import DocumentPayment, FormalDocument
from apps.payment_links.models import CardLink, PaymentLink, PaymentLinkPayment
from apps.rental_billing.models import TenantCharge, TenantStandingOrder
from apps.rentals.tests.factories import make_tenancy
from apps.store.models import StoreInvoice, StoreProduct, StoreSale

IL = ZoneInfo('Asia/Jerusalem')
UTC = ZoneInfo('UTC')
SEP = date(2026, 9, 1)
OCT = date(2026, 10, 1)

TERMINALS = dict(
    TRANZILA_PROD_TERMINAL='fxpmichalweb',
    TRANZILA_PROD_TOKEN_TERMINAL='fxpmichalwebtok',
    TRANZILA_TERMINAL='cogolive',
    TRANZILA_TOKEN_TERMINAL='cogolivetok',
)

INCOMING_URL = '/api/v1/core/dashboard/incoming/'
REFRESH_URL = '/api/v1/core/dashboard/incoming/refresh/'


def il(day: int, month: int = 9, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=IL)


# The rows of the real report (cogolive / cogolivetok, 1.10.2026), as the
# change-impact document records them.
ROW_CHARGE = {
    'index': '101', 'tranmode': 'A', 'txn_type': 'DEBIT', 'processor_response_code': '000',
    'transtatus': '1', 'amount': '35000', 'transaction_date': '2026-09-23', 'transaction_time': '16:30:30',
}
ROW_TOKEN_CHARGE = {**ROW_CHARGE, 'index': '102', 'tranmode': 'AK', 'amount': '500'}
ROW_REFUND = {
    'index': '103', 'tranmode': 'C5', 'txn_type': '', 'processor_response_code': '000',
    'amount': '12000', 'transaction_date': '2026-09-24', 'transaction_time': '09:00:00',
}
ROW_CANCELLED = {**ROW_CHARGE, 'index': '104', 'tranmode': 'D', 'processor_response_code': '800'}
ROW_CARD_CHECK = {**ROW_CHARGE, 'index': '105', 'tranmode': 'N', 'txn_type': ''}
ROW_DECLINED = {**ROW_CHARGE, 'index': '106', 'processor_response_code': '004'}


def no_real_tranzila(testcase) -> None:
    real = patch(
        'apps.core.tranzila_service.TranzilaService._make_api_request',
        side_effect=AssertionError('a real Tranzila request was made'),
    )
    real.start()
    testcase.addCleanup(real.stop)


# --------------------------------------------------------------------- the rule

class PayoutRuleTests(TestCase):
    def test_the_first_of_the_month_waits_for_last_months_money(self):
        payout = upcoming_payout(date(2026, 10, 1))
        self.assertEqual(payout.month, SEP)
        self.assertEqual((payout.period_start, payout.period_end), (date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual(payout.payout_date, date(2026, 10, 6))
        self.assertTrue(payout.is_closed(date(2026, 10, 1)))

    def test_the_sixth_itself_is_still_last_months_transfer(self):
        payout = upcoming_payout(date(2026, 10, 6))
        self.assertEqual(payout.month, SEP)
        self.assertEqual(payout.payout_date, date(2026, 10, 6))

    def test_the_seventh_moves_on_to_the_month_we_are_in(self):
        payout = upcoming_payout(date(2026, 10, 7))
        self.assertEqual(payout.month, OCT)
        self.assertEqual(payout.payout_date, date(2026, 11, 6))
        self.assertFalse(payout.is_closed(date(2026, 10, 7)))

    def test_the_last_day_of_the_month_is_still_open(self):
        payout = upcoming_payout(date(2026, 10, 31))
        self.assertEqual(payout.month, OCT)
        self.assertEqual(payout.period_end, date(2026, 10, 31))
        self.assertEqual(payout.payout_date, date(2026, 11, 6))
        self.assertFalse(payout.is_closed(date(2026, 10, 31)))

    def test_december_is_paid_in_january(self):
        self.assertEqual(payout_for_month(date(2026, 12, 1)).payout_date, date(2027, 1, 6))
        self.assertEqual(payout_for_month(date(2026, 12, 17)).period_end, date(2026, 12, 31))
        late_december = upcoming_payout(date(2026, 12, 20))
        self.assertEqual((late_december.month, late_december.payout_date), (date(2026, 12, 1), date(2027, 1, 6)))
        early_january = upcoming_payout(date(2027, 1, 3))
        self.assertEqual((early_january.month, early_january.payout_date), (date(2026, 12, 1), date(2027, 1, 6)))
        self.assertEqual(upcoming_payout(date(2027, 1, 7)).month, date(2027, 1, 1))

    def test_the_label_is_the_charged_month(self):
        self.assertEqual(payout_for_month(SEP).label, 'ספטמבר 2026')

    @override_settings(CARD_PAYOUT_DAY=10)
    def test_the_day_is_a_setting(self):
        self.assertEqual(upcoming_payout(date(2026, 10, 10)).month, SEP)
        self.assertEqual(upcoming_payout(date(2026, 10, 11)).month, OCT)
        self.assertEqual(payout_for_month(SEP).payout_date, date(2026, 10, 10))

    @override_settings(CARD_PAYOUT_DAY=31)
    def test_a_day_no_month_has_is_held_inside_the_month(self):
        self.assertEqual(payout_for_month(date(2027, 1, 1)).payout_date, date(2027, 2, 28))


# --------------------------------------------------------------------- report rows

class ReportRowTests(TestCase):
    def test_an_approved_charge(self):
        self.assertEqual(classify_report_row(ROW_CHARGE), 'charge')

    def test_a_charge_that_made_a_token(self):
        self.assertEqual(classify_report_row(ROW_TOKEN_CHARGE), 'charge')

    def test_a_credit_is_a_refund(self):
        self.assertEqual(classify_report_row(ROW_REFUND), 'refund')

    def test_a_charge_cancelled_the_same_day_moved_nothing(self):
        self.assertIsNone(classify_report_row(ROW_CANCELLED))

    def test_a_card_check_is_not_money(self):
        self.assertIsNone(classify_report_row(ROW_CARD_CHECK))

    def test_a_decline_is_not_money(self):
        self.assertIsNone(classify_report_row(ROW_DECLINED))
        self.assertIsNone(classify_report_row({**ROW_REFUND, 'processor_response_code': '033'}))

    def test_other_modes_and_broken_rows(self):
        for mode in ('V', 'K', '', None):
            self.assertIsNone(classify_report_row({**ROW_CHARGE, 'tranmode': mode}))
        self.assertIsNone(classify_report_row(None))
        self.assertIsNone(classify_report_row({'tranmode': 'A'}))

    def test_the_month_is_summed_in_shekels(self):
        sums = summarise_report_rows(
            [ROW_CHARGE, ROW_TOKEN_CHARGE, ROW_REFUND, ROW_CANCELLED, ROW_CARD_CHECK, ROW_DECLINED], SEP,
        )
        self.assertEqual(sums['charges_total'], Decimal('355.00'))
        self.assertEqual(sums['charges_count'], 2)
        self.assertEqual(sums['refunds_total'], Decimal('120.00'))
        self.assertEqual(sums['refunds_count'], 1)
        self.assertEqual(sums['installments_count'], 0)

    def test_a_row_read_twice_counts_once(self):
        sums = summarise_report_rows([ROW_CHARGE, dict(ROW_CHARGE)], SEP)
        self.assertEqual((sums['charges_total'], sums['charges_count']), (Decimal('350.00'), 1))

    def test_a_row_of_another_month_is_left_out(self):
        other = {**ROW_CHARGE, 'index': '900', 'transaction_date': '2026-10-01'}
        sums = summarise_report_rows([ROW_CHARGE, other], SEP)
        self.assertEqual(sums['charges_total'], Decimal('350.00'))

    def test_charges_in_payments_are_kept_apart(self):
        split = {
            **ROW_CHARGE, 'index': '200', 'amount': '120000', 'number_of_payments': '3',
            'first_payment_amount': '40000', 'other_payment_amount': '40000',
        }
        sums = summarise_report_rows([ROW_CHARGE, split], SEP)
        self.assertEqual(sums['charges_total'], Decimal('1550.00'))
        self.assertEqual(sums['installments_count'], 1)
        self.assertEqual(sums['installments_total'], Decimal('1200.00'))
        self.assertEqual(sums['installments_first_total'], Decimal('400.00'))


# --------------------------------------------------------------------- the refresh

@override_settings(**TERMINALS)
class RefreshTests(TestCase):
    def setUp(self):
        no_real_tranzila(self)
        self.service = MagicMock(name='tranzila')
        patcher = patch('apps.core.card_payouts.TranzilaService.for_terminal', return_value=self.service)
        self.for_terminal = patcher.start()
        self.addCleanup(patcher.stop)

    def _answers(self, *answers):
        self.service.list_all_transactions.side_effect = list(answers)

    def test_a_month_read_to_its_end(self):
        self._answers(
            {'success': True, 'complete': True, 'transactions': [ROW_REFUND]},
            {'success': True, 'complete': True, 'transactions': []},
            {'success': True, 'complete': True, 'transactions': [ROW_CHARGE, ROW_TOKEN_CHARGE, ROW_CANCELLED]},
        )
        snapshot = refresh_terminal_month('cogolive', SEP, today=date(2026, 10, 1))

        self.for_terminal.assert_called_once_with('cogolive')
        windows = [call.args[:2] for call in self.service.list_all_transactions.call_args_list]
        self.assertEqual(windows, [
            (date(2026, 9, 1), date(2026, 9, 10)),
            (date(2026, 9, 11), date(2026, 9, 20)),
            (date(2026, 9, 21), date(2026, 9, 30)),
        ])
        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.error, '')
        self.assertEqual(snapshot.month, SEP)
        self.assertEqual(snapshot.charges_total, Decimal('355.00'))
        self.assertEqual(snapshot.charges_count, 2)
        self.assertEqual(snapshot.refunds_total, Decimal('120.00'))
        self.assertIsNotNone(snapshot.fetched_at)

    def test_a_partial_read_is_kept_and_marked(self):
        self._answers(
            {'success': True, 'complete': True, 'transactions': [ROW_CHARGE]},
            {'success': True, 'complete': False, 'transactions': [ROW_TOKEN_CHARGE]},
            {'success': True, 'complete': True, 'transactions': []},
        )
        snapshot = refresh_terminal_month('cogolive', SEP, today=date(2026, 10, 1))
        self.assertFalse(snapshot.complete)
        self.assertIn('11/09–20/09', snapshot.error)
        self.assertEqual(snapshot.charges_total, Decimal('355.00'))

    def test_an_error_leaves_no_final_figure(self):
        self._answers(
            {'success': False, 'error': 'Authorization failed', 'transactions': []},
            {'success': True, 'complete': True, 'transactions': [ROW_CHARGE]},
            {'success': True, 'complete': True, 'transactions': []},
        )
        snapshot = refresh_terminal_month('cogolive', SEP, today=date(2026, 10, 1))
        self.assertFalse(snapshot.complete)
        self.assertIn('Authorization failed', snapshot.error)
        self.assertEqual(snapshot.charges_total, Decimal('350.00'))

    def _our_charge(self, terminal, when):
        from apps.customers.models import TranzilaTransaction

        return TranzilaTransaction.objects.create(
            transaction_id='496024', confirmation_code='0012345', transaction_type='recurring_charge',
            response_code='000', response_message='', response_data={}, request_data={},
            is_successful=True, tranzila_terminal=terminal, response_timestamp=when,
        )

    def test_an_empty_report_is_not_believed_when_we_charged_on_that_terminal(self):
        # 1.10.2026: Tranzila answered the michal terminals with an empty list
        # and no error, in a month with hundreds of charges on them.
        from datetime import datetime
        from zoneinfo import ZoneInfo

        self._our_charge('', datetime(2026, 9, 10, 12, 0, tzinfo=ZoneInfo('Asia/Jerusalem')))
        self._answers(*[{'success': True, 'complete': True, 'transactions': []}] * 3)
        snapshot = refresh_terminal_month(TERMINALS['TRANZILA_PROD_TOKEN_TERMINAL'], SEP, today=date(2026, 10, 1))
        self.assertFalse(snapshot.complete)
        self.assertIn('דוח ריק', snapshot.error)
        self.assertIn('1 חיובים', snapshot.error)

    def test_an_empty_report_is_believed_when_we_charged_nothing_there(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # A charge on the michal pair says nothing about cogolivetok.
        self._our_charge('', datetime(2026, 9, 10, 12, 0, tzinfo=ZoneInfo('Asia/Jerusalem')))
        self._answers(*[{'success': True, 'complete': True, 'transactions': []}] * 3)
        snapshot = refresh_terminal_month(TERMINALS['TRANZILA_TOKEN_TERMINAL'], SEP, today=date(2026, 10, 1))
        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.error, '')

    def test_a_crash_is_recorded_not_raised(self):
        self.service.list_all_transactions.side_effect = RuntimeError('boom')
        with self.assertLogs('apps.core.card_payouts', level='ERROR'):
            snapshot = refresh_terminal_month('cogolive', SEP, today=date(2026, 10, 1))
        self.assertFalse(snapshot.complete)
        self.assertIn('boom', snapshot.error)
        self.assertEqual(snapshot.charges_count, 0)

    def test_a_second_read_updates_the_same_row(self):
        self.service.list_all_transactions.return_value = {'success': False, 'error': 'timeout', 'transactions': []}
        refresh_terminal_month('cogolive', SEP, today=date(2026, 10, 1))
        self.service.list_all_transactions.side_effect = [
            {'success': True, 'complete': True, 'transactions': [ROW_CHARGE]},
            {'success': True, 'complete': True, 'transactions': []},
            {'success': True, 'complete': True, 'transactions': []},
        ]
        snapshot = refresh_terminal_month('cogolive', SEP, today=date(2026, 10, 1))
        self.assertEqual(CardPayoutTerminalMonth.objects.count(), 1)
        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.error, '')
        self.assertEqual(snapshot.charges_total, Decimal('350.00'))

    def test_an_open_month_is_read_up_to_today(self):
        self.service.list_all_transactions.return_value = {'success': True, 'complete': True, 'transactions': []}
        refresh_terminal_month('cogolivetok', OCT, today=date(2026, 10, 12))
        windows = [call.args[:2] for call in self.service.list_all_transactions.call_args_list]
        self.assertEqual(windows, [
            (date(2026, 10, 1), date(2026, 10, 10)),
            (date(2026, 10, 11), date(2026, 10, 12)),
        ])

    def test_a_terminal_that_is_not_ours_is_refused(self):
        with self.assertRaises(PayoutError) as raised:
            refresh_terminal_month('someoneelse', SEP, today=date(2026, 10, 1))
        self.assertIn('someoneelse', str(raised.exception))
        self.for_terminal.assert_not_called()
        self.assertEqual(CardPayoutTerminalMonth.objects.count(), 0)

    def test_a_month_that_has_not_started_is_refused(self):
        with self.assertRaises(PayoutError):
            refresh_terminal_month('cogolive', date(2026, 11, 1), today=date(2026, 10, 1))
        self.service.list_all_transactions.assert_not_called()

    def test_a_terminal_without_keys_is_marked(self):
        self.for_terminal.return_value = None
        snapshot = refresh_terminal_month('cogolive', SEP, today=date(2026, 10, 1))
        self.assertFalse(snapshot.complete)
        self.assertTrue(snapshot.error)


class PayoutTerminalsTests(TestCase):
    @override_settings(**TERMINALS)
    def test_the_four_terminals_with_labels(self):
        rows = payout_terminals()
        self.assertEqual(
            [row['terminal'] for row in rows], ['fxpmichalweb', 'fxpmichalwebtok', 'cogolive', 'cogolivetok'],
        )
        self.assertTrue(all(row['label'] for row in rows))

    @override_settings(**{**TERMINALS, 'TRANZILA_TOKEN_TERMINAL': 'cogolive'})
    def test_two_settings_on_one_terminal_give_one_row(self):
        self.assertEqual([row['terminal'] for row in payout_terminals()], ['fxpmichalweb', 'fxpmichalwebtok', 'cogolive'])

    @override_settings(
        TRANZILA_PROD_TERMINAL='', TRANZILA_PROD_TOKEN_TERMINAL='fxpmichalwebtok',
        TRANZILA_TERMINAL='mock-terminal', TRANZILA_TOKEN_TERMINAL='mock-token-terminal',
    )
    def test_placeholders_and_blanks_are_left_out(self):
        self.assertEqual([row['terminal'] for row in payout_terminals()], ['fxpmichalwebtok'])


# --------------------------------------------------------------------- ours

class MoneyFixture:
    """Two branches, a child with a lesson in the first, and makers for every kind of card money."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.branch = TestDataFactory.create_branch(name='פלורנטין')
        self.other = TestDataFactory.create_branch(name='רמת גן')
        self.child = TestDataFactory.create_child()
        self.lesson = TestDataFactory.create_lesson(branch=self.branch)
        self._numbers = 0

    def _number(self, prefix: str) -> str:
        self._numbers += 1
        return f'{prefix}-{self._numbers:05d}'

    def source(self, result: dict, key: str) -> dict:
        return next(row for row in result['by_source'] if row['key'] == key)

    def branch_row(self, result: dict, branch) -> dict:
        return next(row for row in result['by_branch'] if row['branch_id'] == str(branch.id))

    def payment(self, amount, when, *, branch='default', status='completed', lesson='default'):
        return Payment.objects.create(
            child=self.child, family=self.child.family,
            branch=self.branch if branch == 'default' else branch,
            lesson=self.lesson if lesson == 'default' else lesson,
            payment_type='recurring_subscription', status=status,
            base_amount=Decimal(amount), final_amount=Decimal(amount), payment_date=when,
        )

    def refund_row(self, key: str, amount, when, *, successful=True):
        return TranzilaTransaction.objects.create(
            transaction_id='R1' if successful else '', transaction_type='refund',
            request_data={'amount': str(amount)}, idempotency_key=key,
            is_successful=successful, response_timestamp=when if successful else None,
        )

    def store_invoice(self, total, when, lines, *, method='credit_card', status='completed', branch=None):
        """`lines` is [(amount, branch or None)]; the sale lines are written at `when`, as a payment writes them."""
        product = StoreProduct.objects.create(
            name=f'חולצה {self._number("p")}', category='ביגוד', sale_price=Decimal('10.00'),
            cost_price=Decimal('4.00'), stock_quantity=50,
        )
        invoice = StoreInvoice.objects.create(
            customer_name='קונה', total_amount=Decimal(total), payment_method=method, payment_status=status,
            branch=branch,
        )
        StoreInvoice.objects.filter(pk=invoice.pk).update(issue_date=when)
        for amount, line_branch in lines:
            sale = StoreSale.objects.create(
                invoice=invoice, product=product, quantity=1, unit_price=Decimal(amount),
                total_price=Decimal(amount), payment_method=method, branch=line_branch,
            )
            StoreSale.objects.filter(pk=sale.pk).update(sale_date=when)
        return StoreInvoice.objects.get(pk=invoice.pk)

    def link_payment(self, amount, when, *, branch='default', status=PaymentLinkPayment.STATUS_COMPLETED,
                     kind=PaymentLink.KIND_GENERAL):
        link = PaymentLink.objects.create(
            title='סדנה', kind=kind, branch=self.branch if branch == 'default' else branch,
        )
        return PaymentLinkPayment.objects.create(
            link=link, amount=Decimal(amount), payer_name='משלם', status=status, paid_at=when,
        )

    def document(self, total, day, lines, *, document_type='combined', branch='default', notes=''):
        """`lines` is [(method, amount, paid_on or None)]."""
        doc = FormalDocument.objects.create(
            document_number=self._number('DOC'), document_type=document_type, client_type='business',
            document_date=day, total_amount=Decimal(total), subtotal=Decimal(total),
            branch=self.branch if branch == 'default' else branch, internal_notes=notes,
        )
        for method, amount, paid_on in lines:
            DocumentPayment.objects.create(document=doc, payment_method=method, amount=Decimal(amount), paid_on=paid_on)
        return doc

    def tenant_charge(self, when, *, period=SEP, total=145678, branch='default'):
        branch = self.branch if branch == 'default' else branch
        tenancy = make_tenancy(branch)
        order = TenantStandingOrder.objects.create(
            tenancy=tenancy, tenant=tenancy.tenant, branch=branch, amount_before_vat=Decimal('1234.56'),
            billing_day=10, start_date=date(2026, 9, 1), status=TenantStandingOrder.STATUS_ACTIVE,
        )
        business, _ = Business.objects.get_or_create(name='סוחרים')
        return TenantCharge.objects.create(
            standing_order=order, tenancy=tenancy, period=period, amount_before_vat=total - 22222,
            vat_amount=22222, total=total, business=business, status=TenantCharge.STATUS_CHARGED,
            trigger=TenantCharge.TRIGGER_CRON, transaction_id='T100', charged_at=when,
        )


class CoursePaymentsTests(MoneyFixture, TestCase):
    def test_a_months_charges(self):
        self.payment('300.00', il(5))
        self.payment('250.00', il(28))
        self.payment('999.00', il(3, month=10))
        result = our_card_money(SEP)
        courses = self.source(result, 'courses')
        self.assertEqual(courses['charges'], Decimal('550.00'))
        self.assertEqual(courses['count'], 2)
        self.assertEqual(result['net'], Decimal('550.00'))
        self.assertEqual(self.branch_row(result, self.branch)['net'], Decimal('550.00'))
        self.assertEqual(our_card_money(OCT)['net'], Decimal('999.00'))

    def test_only_money_that_moved(self):
        self.payment('300.00', il(5))
        for status in ('pending', 'processing', 'failed', 'cancelled'):
            self.payment('111.00', il(6), status=status)
        self.assertEqual(our_card_money(SEP)['net'], Decimal('300.00'))

    def test_the_month_is_israels(self):
        # 30.9 22:30 UTC is already 1.10 in Israel; 31.8 21:30 UTC is already 1.9.
        self.payment('100.00', datetime(2026, 9, 30, 22, 30, tzinfo=UTC))
        self.payment('40.00', datetime(2026, 8, 31, 21, 30, tzinfo=UTC))
        self.assertEqual(our_card_money(SEP)['net'], Decimal('40.00'))
        self.assertEqual(our_card_money(OCT)['net'], Decimal('100.00'))

    def test_a_partial_refund_takes_only_its_own_sum(self):
        payment = self.payment('300.00', il(5), status='refunded')
        self.refund_row(f'refund_claim_payment_{payment.id}', '100.00', il(20))
        result = our_card_money(SEP)
        self.assertEqual(result['charges'], Decimal('300.00'))
        self.assertEqual(result['refunds'], Decimal('100.00'))
        self.assertEqual(result['net'], Decimal('200.00'))
        self.assertEqual(self.source(result, 'courses')['net'], Decimal('200.00'))

    def test_a_refund_in_a_later_month_reduces_that_month(self):
        payment = self.payment('300.00', il(5), status='refunded')
        self.refund_row(f'refund_claim_payment_{payment.id}', '300.00', il(2, month=10))
        self.assertEqual(our_card_money(SEP)['net'], Decimal('300.00'))
        october = our_card_money(OCT)
        self.assertEqual((october['charges'], october['refunds'], october['net']),
                         (Decimal('0.00'), Decimal('300.00'), Decimal('-300.00')))

    def test_a_refund_row_of_the_older_shape(self):
        payment = self.payment('300.00', il(5), status='refunded')
        self.refund_row(f'refund_payment_{payment.id}_778899', '300.00', il(9))
        self.assertEqual(our_card_money(SEP)['net'], Decimal('0.00'))

    def test_a_refund_with_no_answer_is_not_a_refund_yet(self):
        payment = self.payment('300.00', il(5))
        self.refund_row(f'refund_claim_payment_{payment.id}', '300.00', il(9), successful=False)
        self.assertEqual(our_card_money(SEP)['net'], Decimal('300.00'))

    def test_a_card_check_at_signup_adds_nothing(self):
        self.payment('0.00', il(5))
        result = our_card_money(SEP)
        self.assertEqual(result['net'], Decimal('0.00'))
        self.assertEqual(self.source(result, 'courses')['count'], 0)

    def test_money_without_a_branch_has_its_own_line(self):
        self.payment('300.00', il(5))
        self.payment('80.00', il(6), branch=None)
        result = our_card_money(SEP)
        self.assertEqual(result['net'], Decimal('380.00'))
        self.assertEqual(result['no_branch']['net'], Decimal('80.00'))
        self.assertEqual([row['branch_id'] for row in result['by_branch']], [str(self.branch.id)])

    def test_a_one_time_card_link_charge_is_a_link(self):
        payment = self.payment('150.00', il(7), lesson=None)
        Payment.objects.filter(pk=payment.pk).update(payment_type='one_time')
        CardLink.objects.create(kind=CardLink.KIND_ONE_TIME, child=self.child, payment=payment,
                                amount=Decimal('150.00'), branch=self.branch)
        result = our_card_money(SEP)
        self.assertEqual(self.source(result, 'links')['net'], Decimal('150.00'))
        self.assertEqual(self.source(result, 'courses')['net'], Decimal('0.00'))
        self.assertEqual(result['net'], Decimal('150.00'))


class StoreTests(MoneyFixture, TestCase):
    def test_each_line_to_its_branch_and_the_delivery_fee_to_none(self):
        self.store_invoice('130.00', il(10), [('60.00', self.branch), ('40.00', self.other), ('10.00', None)])
        result = our_card_money(SEP)
        self.assertEqual(self.source(result, 'store')['net'], Decimal('130.00'))
        self.assertEqual(self.source(result, 'store')['count'], 1)
        self.assertEqual(self.branch_row(result, self.branch)['net'], Decimal('60.00'))
        self.assertEqual(self.branch_row(result, self.other)['net'], Decimal('40.00'))
        # The website line with no branch, and the ₪20 the invoice charged beyond its lines.
        self.assertEqual(result['no_branch']['net'], Decimal('30.00'))

    def test_only_card_sales_that_were_paid(self):
        self.store_invoice('50.00', il(10), [('50.00', self.branch)])
        self.store_invoice('70.00', il(10), [('70.00', self.branch)], method='cash')
        self.store_invoice('90.00', il(10), [('90.00', self.branch)], method='monthly_billing')
        self.store_invoice('30.00', il(10), [], status='pending')
        self.store_invoice('20.00', il(10), [], status='failed')
        self.assertEqual(our_card_money(SEP)['net'], Decimal('50.00'))

    def test_a_purchase_on_the_standing_order_is_counted_in_the_charge_only(self):
        # The shirt rides on the month's standing-order charge: one Payment of 300 + 90.
        self.store_invoice('90.00', il(10), [('90.00', self.branch)], method='monthly_billing')
        self.payment('390.00', il(1, month=10))
        self.assertEqual(our_card_money(SEP)['net'], Decimal('0.00'))
        october = our_card_money(OCT)
        self.assertEqual(october['net'], Decimal('390.00'))
        self.assertEqual(self.source(october, 'store')['net'], Decimal('0.00'))

    def test_the_month_is_when_it_was_paid_not_when_the_order_was_opened(self):
        invoice = self.store_invoice('50.00', il(30, hour=23, minute=50), [])
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_reported_at=il(1, month=10, hour=0, minute=5))
        self.assertEqual(our_card_money(SEP)['net'], Decimal('0.00'))
        self.assertEqual(our_card_money(OCT)['net'], Decimal('50.00'))

    def test_a_partial_refund_is_shared_as_the_sale_was(self):
        invoice = self.store_invoice('100.00', il(10), [('75.00', self.branch), ('25.00', self.other)], status='refunded')
        self.refund_row(f'refund_claim_store_{invoice.id}', '40.00', il(12))
        result = our_card_money(SEP)
        store = self.source(result, 'store')
        self.assertEqual((store['charges'], store['refunds'], store['net']),
                         (Decimal('100.00'), Decimal('40.00'), Decimal('60.00')))
        self.assertEqual(self.branch_row(result, self.branch)['refunds'], Decimal('30.00'))
        self.assertEqual(self.branch_row(result, self.other)['refunds'], Decimal('10.00'))


class LinksRentalsDocumentsTests(MoneyFixture, TestCase):
    def test_a_link_payment(self):
        self.link_payment('200.00', il(8))
        self.link_payment('55.00', il(8), status=PaymentLinkPayment.STATUS_REVIEW)
        self.link_payment('66.00', il(8), status=PaymentLinkPayment.STATUS_FAILED)
        self.link_payment('77.00', il(8), branch=None)
        result = our_card_money(SEP)
        self.assertEqual(self.source(result, 'links')['net'], Decimal('277.00'))
        self.assertEqual(result['no_branch']['net'], Decimal('77.00'))

    def test_a_business_charge_and_its_document_are_one_payment(self):
        row = self.link_payment('500.00', il(8), kind=PaymentLink.KIND_BUSINESS_CHARGE)
        row.formal_document = self.document('500.00', date(2026, 9, 8), [('credit_card', '500.00', date(2026, 9, 8))])
        row.save(update_fields=['formal_document'])
        result = our_card_money(SEP)
        self.assertEqual(result['net'], Decimal('500.00'))
        self.assertEqual(self.source(result, 'links')['net'], Decimal('500.00'))
        self.assertEqual(self.source(result, 'manual_documents')['net'], Decimal('0.00'))

    def test_a_rental_charged_by_card_and_its_receipt_are_one_payment(self):
        charge = self.tenant_charge(il(10))
        charge.receipt = self.document('1456.78', date(2026, 9, 10), [('credit_card', '1456.78', date(2026, 9, 10))])
        charge.save(update_fields=['receipt'])
        result = our_card_money(SEP)
        self.assertEqual(result['net'], Decimal('1456.78'))
        self.assertEqual(self.source(result, 'rentals')['net'], Decimal('1456.78'))
        self.assertEqual(self.source(result, 'manual_documents')['net'], Decimal('0.00'))
        self.assertEqual(self.branch_row(result, self.branch)['net'], Decimal('1456.78'))

    def test_a_rental_paid_at_the_office_is_not_card_money(self):
        for method in ('cash', 'check', 'bank_transfer'):
            charge = self.tenant_charge(il(10), branch=TestDataFactory.create_branch(name=f'סניף {method}'))
            charge.receipt = self.document('1456.78', date(2026, 9, 10), [(method, '1456.78', date(2026, 9, 10))])
            charge.save(update_fields=['receipt'])
        self.assertEqual(our_card_money(SEP)['net'], Decimal('0.00'))

    def test_only_a_rental_month_that_was_charged(self):
        charge = self.tenant_charge(il(10))
        TenantCharge.objects.filter(pk=charge.pk).update(status=TenantCharge.STATUS_REVIEW)
        self.assertEqual(our_card_money(SEP)['net'], Decimal('0.00'))

    def test_a_document_typed_by_hand_counts_its_card_line_only(self):
        self.document('900.00', date(2026, 9, 15), [('credit_card', '600.00', None), ('cash', '300.00', None)])
        self.document('400.00', date(2026, 9, 16), [('check', '400.00', None)], document_type='receipt')
        self.document('250.00', date(2026, 9, 17), [('credit_card', '250.00', None)], document_type='receipt')
        self.document('111.00', date(2026, 9, 18), [('credit_card', '111.00', None)], document_type='draft')
        result = our_card_money(SEP)
        manual = self.source(result, 'manual_documents')
        self.assertEqual(manual['net'], Decimal('850.00'))
        self.assertEqual(manual['count'], 2)
        self.assertTrue(manual['note'])

    def test_a_document_line_is_dated_by_its_payment_day(self):
        self.document('300.00', date(2026, 10, 2), [('credit_card', '300.00', date(2026, 9, 29))])
        self.assertEqual(our_card_money(SEP)['net'], Decimal('300.00'))
        self.assertEqual(our_card_money(OCT)['net'], Decimal('0.00'))

    def test_a_store_sales_document_is_not_counted_again(self):
        invoice = self.store_invoice('50.00', il(10), [('50.00', self.branch)])
        invoice.formal_document = self.document('50.00', date(2026, 9, 10), [('credit_card', '50.00', None)])
        invoice.save(update_fields=['formal_document'])
        result = our_card_money(SEP)
        self.assertEqual(result['net'], Decimal('50.00'))
        self.assertEqual(self.source(result, 'manual_documents')['net'], Decimal('0.00'))

    def test_michal_kagans_site_is_its_own_source(self):
        self.document('350.00', date(2026, 9, 12), [('credit_card', '350.00', None)], branch=None,
                      notes='michal-payment:pay_1\nהופק אוטומטית לבקשת האתר של מיכל קגן')
        self.document('120.00', date(2026, 9, 20), [], document_type='credit_invoice', branch=None,
                      notes='michal-refund:ref_1\nהופק אוטומטית עם החזר באתר של מיכל קגן')
        # A credit note typed by hand names no means of payment: never a card refund.
        self.document('75.00', date(2026, 9, 21), [], document_type='credit_invoice')
        result = our_card_money(SEP)
        michal = self.source(result, 'michal')
        self.assertEqual((michal['charges'], michal['refunds'], michal['net']),
                         (Decimal('350.00'), Decimal('120.00'), Decimal('230.00')))
        self.assertEqual(self.source(result, 'manual_documents')['net'], Decimal('0.00'))
        self.assertEqual(result['no_branch']['net'], Decimal('230.00'))
        self.assertEqual(result['net'], Decimal('230.00'))


class BranchFilterTests(MoneyFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.payment('300.00', il(5))
        self.payment('120.00', il(6), branch=self.other)
        self.payment('80.00', il(6), branch=None)
        self.store_invoice('100.00', il(10), [('60.00', self.branch), ('40.00', self.other)])
        self.link_payment('200.00', il(8), branch=self.other)
        self.tenant_charge(il(10))
        self.document('350.00', date(2026, 9, 12), [('credit_card', '350.00', None)], branch=None,
                      notes='michal-payment:pay_1\n')
        self.document('500.00', date(2026, 9, 12), [('credit_card', '500.00', None)], branch=self.other)

    def test_the_whole_company(self):
        result = our_card_money(SEP)
        self.assertEqual(result['net'], Decimal('3106.78'))
        self.assertEqual(result['no_branch']['net'], Decimal('430.00'))
        self.assertEqual(sum(row['net'] for row in result['by_source']), result['net'])
        self.assertEqual(sum(row['net'] for row in result['by_branch']) + result['no_branch']['net'], result['net'])

    def test_one_branch(self):
        result = our_card_money(SEP, [self.branch.id])
        self.assertEqual(result['net'], Decimal('1816.78'))
        self.assertIsNone(result['no_branch'])
        self.assertEqual([row['branch_id'] for row in result['by_branch']], [str(self.branch.id)])
        self.assertEqual(self.source(result, 'michal')['net'], Decimal('0.00'))
        self.assertEqual(self.source(result, 'store')['net'], Decimal('60.00'))

    def test_the_other_branch(self):
        result = our_card_money(SEP, [str(self.other.id)])
        self.assertEqual(result['net'], Decimal('860.00'))
        self.assertEqual(self.source(result, 'manual_documents')['net'], Decimal('500.00'))

    def test_no_branches_is_nothing(self):
        result = our_card_money(SEP, [])
        self.assertEqual(result['net'], Decimal('0.00'))
        self.assertEqual(result['by_branch'], [])
        self.assertIsNone(result['no_branch'])


# --------------------------------------------------------------------- the endpoints

@override_settings(**TERMINALS)
class IncomingEndpointTests(MoneyFixture, TestCase):
    def setUp(self):
        super().setUp()
        no_real_tranzila(self)
        today = patch('apps.core.card_payouts.israel_today', return_value=date(2026, 10, 3))
        today.start()
        self.addCleanup(today.stop)

        self.payment('300.00', il(5))
        self.payment('120.00', il(6), branch=self.other)
        self.payment('80.00', il(6), branch=None)
        self.payment('45.00', il(2, month=10))

        self.manager = TestDataFactory.create_user('payout-manager@test', UserProfile.ROLE_MANAGER)
        self.partner = TestDataFactory.create_user('payout-partner@test', UserProfile.ROLE_PARTNER)
        self.partner.profile.assigned_branches.set([self.branch])
        self.worker = TestDataFactory.create_user('payout-worker@test', UserProfile.ROLE_WORKER)

    def client_for(self, user) -> APIClient:
        client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        return client

    def snapshot(self, terminal, *, charges='0.00', refunds='0.00', complete=True, month=SEP, **fields):
        return CardPayoutTerminalMonth.objects.create(
            terminal=terminal, month=month, charges_total=Decimal(charges), refunds_total=Decimal(refunds),
            complete=complete, fetched_at=il(1, month=10), **fields,
        )

    def test_a_manager_sees_the_upcoming_transfer(self):
        response = self.client_for(self.manager).get(INCOMING_URL)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['month'], '2026-09')
        self.assertEqual(body['payout_date'], '2026-10-06')
        self.assertEqual(body['period'], {'start': '2026-09-01', 'end': '2026-09-30', 'label': 'ספטמבר 2026'})
        self.assertTrue(body['is_closed'])
        self.assertEqual(body['ours']['total'], 500.0)
        self.assertEqual(body['ours']['no_branch']['net'], 80.0)
        self.assertEqual({row['branch_name'] for row in body['ours']['by_branch']}, {'פלורנטין', 'רמת גן'})
        self.assertIn('לפני עמלות', body['note'])
        self.assertEqual(body['next']['payout_date'], '2026-11-06')
        self.assertEqual(body['next']['total'], 45.0)
        self.assertFalse(body['next']['is_closed'])

    def test_a_chosen_month(self):
        body = self.client_for(self.manager).get(INCOMING_URL, {'month': '2026-10'}).json()
        self.assertEqual(body['payout_date'], '2026-11-06')
        self.assertFalse(body['is_closed'])
        self.assertEqual(body['ours']['total'], 45.0)
        # November has not begun: there is no "transfer after it" to speak of yet.
        self.assertIsNone(body['next'])

    def test_a_manager_gets_tranzilas_side_from_the_snapshots(self):
        self.snapshot('fxpmichalweb', charges='100.00')
        self.snapshot('fxpmichalwebtok', charges='400.00', refunds='20.00', installments_total=Decimal('60.00'))
        self.snapshot('cogolive', charges='50.00')
        self.snapshot('cogolivetok')
        with patch('apps.core.card_payouts.TranzilaService.for_terminal',
                   side_effect=AssertionError('the GET must not reach Tranzila')):
            body = self.client_for(self.manager).get(INCOMING_URL).json()
        tranzila = body['tranzila']
        self.assertEqual([row['terminal'] for row in tranzila['terminals']],
                         ['fxpmichalweb', 'fxpmichalwebtok', 'cogolive', 'cogolivetok'])
        token = tranzila['terminals'][1]
        self.assertEqual((token['charges'], token['refunds'], token['net']), (400.0, 20.0, 380.0))
        self.assertTrue(token['label'])
        self.assertTrue(tranzila['complete'])
        self.assertEqual(tranzila['total'], 530.0)
        self.assertEqual(tranzila['gap'], 30.0)
        self.assertEqual(tranzila['installments_total'], 60.0)

    def test_no_gap_while_a_terminal_is_unread_or_partial(self):
        self.snapshot('fxpmichalweb', charges='100.00')
        self.snapshot('fxpmichalwebtok', charges='400.00', complete=False, error='11/09–20/09: timeout')
        tranzila = self.client_for(self.manager).get(INCOMING_URL).json()['tranzila']
        self.assertFalse(tranzila['complete'])
        self.assertIsNone(tranzila['gap'])
        self.assertEqual(tranzila['total'], 500.0)
        unread = tranzila['terminals'][2]
        self.assertEqual((unread['terminal'], unread['fetched_at'], unread['net']), ('cogolive', None, None))
        self.assertEqual(tranzila['terminals'][1]['error'], '11/09–20/09: timeout')

    def test_a_branch_filter_has_no_tranzila_side(self):
        body = self.client_for(self.manager).get(INCOMING_URL, {'branch_id': str(self.other.id)}).json()
        self.assertNotIn('tranzila', body)
        self.assertEqual(body['ours']['total'], 120.0)
        self.assertIsNone(body['ours']['no_branch'])

    def test_a_partner_sees_only_their_branches(self):
        self.snapshot('fxpmichalweb', charges='100.00')
        body = self.client_for(self.partner).get(INCOMING_URL).json()
        self.assertEqual(body['ours']['total'], 300.0)
        self.assertEqual([row['branch_id'] for row in body['ours']['by_branch']], [str(self.branch.id)])
        self.assertIsNone(body['ours']['no_branch'])
        self.assertNotIn('tranzila', body)
        self.assertEqual(body['next']['total'], 45.0)

    def test_a_partner_asking_for_their_own_branch(self):
        body = self.client_for(self.partner).get(INCOMING_URL, {'branch_id': str(self.branch.id)}).json()
        self.assertEqual(body['ours']['total'], 300.0)

    def test_a_partner_asking_for_a_branch_that_is_not_theirs_gets_nothing(self):
        body = self.client_for(self.partner).get(INCOMING_URL, {'branch_id': str(self.other.id)}).json()
        self.assertEqual(body['ours']['total'], 0.0)
        self.assertEqual(body['ours']['by_branch'], [])
        self.assertEqual(body['next']['total'], 0.0)
        self.assertNotIn('tranzila', body)

    def test_a_partner_with_no_branches_gets_nothing(self):
        self.partner.profile.assigned_branches.clear()
        body = self.client_for(self.partner).get(INCOMING_URL).json()
        self.assertEqual(body['ours']['total'], 0.0)

    def test_a_worker_is_refused(self):
        self.assertEqual(self.client_for(self.worker).get(INCOMING_URL).status_code, 403)
        self.assertEqual(self.client_for(self.worker).post(REFRESH_URL, {}, format='json').status_code, 403)

    def test_nobody_signed_in_is_refused(self):
        self.assertEqual(APIClient().get(INCOMING_URL).status_code, 401)

    def test_a_month_or_a_branch_that_cannot_be_read(self):
        client = self.client_for(self.manager)
        self.assertEqual(client.get(INCOMING_URL, {'month': '9/2026'}).status_code, 400)
        self.assertEqual(client.get(INCOMING_URL, {'branch_id': 'delivery'}).status_code, 400)
        self.assertEqual(client.get(INCOMING_URL, {'branch_id': 'all'}).status_code, 200)

    def test_only_a_manager_refreshes(self):
        with patch('apps.core.card_payouts.refresh_terminal_month') as refresh:
            response = self.client_for(self.partner).post(
                REFRESH_URL, {'month': '2026-09', 'terminal': 'cogolive'}, format='json',
            )
        self.assertEqual(response.status_code, 403)
        refresh.assert_not_called()

    def test_a_refresh_reads_one_terminal_and_answers_its_row(self):
        service = MagicMock(name='tranzila')
        service.list_all_transactions.return_value = {'success': True, 'complete': True, 'transactions': [ROW_CHARGE]}
        with patch('apps.core.card_payouts.TranzilaService.for_terminal', return_value=service) as for_terminal:
            response = self.client_for(self.manager).post(
                REFRESH_URL, {'month': '2026-09', 'terminal': 'cogolive'}, format='json',
            )
        self.assertEqual(response.status_code, 200)
        for_terminal.assert_called_once_with('cogolive')
        body = response.json()
        self.assertEqual((body['terminal'], body['charges'], body['net'], body['count']), ('cogolive', 350.0, 350.0, 1))
        self.assertTrue(body['complete'])
        self.assertTrue(body['label'])
        self.assertEqual(CardPayoutTerminalMonth.objects.get().terminal, 'cogolive')

    def test_a_refresh_that_cannot_be_asked(self):
        client = self.client_for(self.manager)
        unknown = client.post(REFRESH_URL, {'month': '2026-09', 'terminal': 'someoneelse'}, format='json')
        self.assertEqual(unknown.status_code, 400)
        self.assertIn('someoneelse', unknown.json()['error'])
        self.assertEqual(client.post(REFRESH_URL, {'terminal': 'cogolive'}, format='json').status_code, 400)
        future = client.post(REFRESH_URL, {'month': '2026-12', 'terminal': 'cogolive'}, format='json')
        self.assertEqual(future.status_code, 400)
        self.assertEqual(CardPayoutTerminalMonth.objects.count(), 0)


# --------------------------------------------------------------------- the brief

@override_settings(**TERMINALS)
class BriefItemTests(MoneyFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.payment('1300.00', il(5))
        self.payment('45.00', il(2, month=10))

    def test_before_the_payout_day_it_says_what_arrives(self):
        from apps.core.daily_brief import GREEN, check_card_payout

        item = check_card_payout(date(2026, 10, 3))
        self.assertEqual((item.key, item.severity), ('card_payout', GREEN))
        self.assertIn('ב־6.10 צפוי להיכנס 1,300 ₪', item.summary)
        self.assertIn('גבייה של ספטמבר 2026', item.summary)
        self.assertNotIn('בטרנזילה', item.summary)

    def test_with_every_terminal_read_it_adds_tranzilas_figure_and_the_gap(self):
        from apps.core.daily_brief import GREEN, check_card_payout

        for terminal, charges in (('fxpmichalweb', '0.00'), ('fxpmichalwebtok', '1350.00'),
                                  ('cogolive', '0.00'), ('cogolivetok', '0.00')):
            CardPayoutTerminalMonth.objects.create(
                terminal=terminal, month=SEP, charges_total=Decimal(charges), complete=True, fetched_at=il(1, month=10),
            )
        item = check_card_payout(date(2026, 10, 6))
        self.assertEqual(item.severity, GREEN)
        self.assertIn('בטרנזילה 1,350 ₪', item.summary)
        self.assertIn('פער 50 ₪ — בטרנזילה יותר', item.summary)
        self.assertEqual(len(item.rows), 4)

    def test_the_gap_says_which_side_has_more(self):
        from apps.core.daily_brief import check_card_payout

        for terminal, charges in (('fxpmichalweb', '0.00'), ('fxpmichalwebtok', '1200.00'),
                                  ('cogolive', '0.00'), ('cogolivetok', '0.00')):
            CardPayoutTerminalMonth.objects.create(
                terminal=terminal, month=SEP, charges_total=Decimal(charges), complete=True, fetched_at=il(1, month=10),
            )
        self.assertIn('פער 100 ₪ — אצלנו יותר', check_card_payout(date(2026, 10, 3)).summary)
        CardPayoutTerminalMonth.objects.filter(terminal='cogolive').update(charges_total=Decimal('100.00'))
        self.assertIn('אין פער', check_card_payout(date(2026, 10, 3)).summary)

    def test_a_partial_read_is_not_quoted(self):
        from apps.core.daily_brief import check_card_payout

        CardPayoutTerminalMonth.objects.create(
            terminal='fxpmichalwebtok', month=SEP, charges_total=Decimal('900.00'), complete=False,
            fetched_at=il(1, month=10),
        )
        item = check_card_payout(date(2026, 10, 3))
        self.assertNotIn('בטרנזילה', item.summary)

    def test_after_the_payout_day_it_is_one_quiet_line(self):
        from apps.core.daily_brief import GREEN, check_card_payout

        item = check_card_payout(date(2026, 10, 7))
        self.assertEqual(item.severity, GREEN)
        self.assertIn('ההעברה הבאה ב־6.11', item.summary)
        self.assertIn('45 ₪', item.summary)
        self.assertEqual(item.rows, [])

    def test_it_is_one_of_the_briefs_checks(self):
        from apps.core.daily_brief import CHECK_REGISTRY, EXTERNAL_CHECKS, check_catalogue, run_check

        self.assertIn('card_payout', CHECK_REGISTRY)
        self.assertNotIn('card_payout', EXTERNAL_CHECKS)
        entry = next(row for row in check_catalogue() if row['key'] == 'card_payout')
        self.assertEqual(entry['title'], 'כסף שעומד להיכנס')
        self.assertEqual(run_check('card_payout', today=date(2026, 10, 3))['severity'], 'green')
