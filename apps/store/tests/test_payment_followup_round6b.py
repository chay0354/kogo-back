"""
Stage 3, sixth round, part 2: what counts as a complete read of the report
(probes P4, P4b, P4c), an old payment link with no terminal (P5), the website
store's own switch, and what the site reads while a retry is refused a page.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from apps.core.payment_service import PaymentService
from apps.core.tranzila_service import TranzilaService
from apps.payment_links.models import PaymentLink, PaymentLinkOption, PaymentLinkPayment
from apps.payment_links.public_views import _index_paid_for_something_else
from apps.store.models import StoreInvoice
from apps.store.tests.test_payment_followup import FollowupBase, paid_row
from apps.store.tests.test_payment_followup_round4 import _opened, row_at
from apps.store.tests.test_payment_followup_round5 import Base, held, unpace


class CompleteReadTest(FollowupBase):
    def test_P4_fewer_rows_than_the_answers_own_total_is_not_complete(self):
        today = date.today()
        rows = [paid_row(index=str(1000 + i)) for i in range(100)]
        with patch.object(TranzilaService, '_make_api_request',
                          return_value={'error_code': 0, 'message': 'Success', 'transactions': rows, 'total': 250}):
            raw = TranzilaService.iframe().list_all_transactions(today, today, max_pages=3)
        self.assertIs(raw['complete'], False)

    def test_all_the_rows_the_answer_counts_is_complete(self):
        today = date.today()
        rows = [paid_row(index=str(1000 + i)) for i in range(100)]
        with patch.object(TranzilaService, '_make_api_request',
                          return_value={'error_code': 0, 'message': 'Success', 'transactions': rows, 'total': 100}):
            raw = TranzilaService.iframe().list_all_transactions(today, today, max_pages=3)
        self.assertIs(raw['complete'], True)

    def test_P4b_an_answer_with_no_error_code_value_is_not_read_as_a_list(self):
        today = date.today()
        with patch.object(TranzilaService, '_make_api_request',
                          return_value={'error_code': None, 'transactions': [paid_row()]}):
            raw = TranzilaService.iframe().list_all_transactions(today, today, max_pages=3)
        self.assertFalse(raw['success'])

    def test_P4c_an_error_envelope_with_an_empty_list_is_not_no_such_transaction(self):
        with patch.object(TranzilaService, '_make_api_request',
                          return_value={'error_code': 20002, 'message': 'Invalid application key', 'transactions': []}):
            found = TranzilaService.iframe().find_transaction('123456')
        self.assertFalse(found['success'])

    def test_a_good_answer_with_an_empty_list_is_no_such_transaction(self):
        with patch.object(TranzilaService, '_make_api_request',
                          return_value={'error_code': 0, 'message': 'Success', 'transactions': []}):
            found = TranzilaService.iframe().find_transaction('123456')
        self.assertEqual(found, {'success': True, 'transaction': None})


class OldPaymentLinkTest(Base):
    def _old_link_payment(self, index, *, paid_at, terminal=''):
        user = get_user_model().objects.create_user(username='o@example.com', email='o@example.com', password='x12345678!')
        link = PaymentLink.objects.create(title='ישן', created_by=user)
        option = PaymentLinkOption.objects.create(link=link, label='כרטיס', amount=Decimal('50'))
        return PaymentLinkPayment.objects.create(
            link=link, option=option, option_label='כרטיס', amount=Decimal('50'), payer_name='ישן',
            status=PaymentLinkPayment.STATUS_COMPLETED, gateway_transaction_id=index, tranzila_terminal=terminal,
            paid_at=paid_at)

    def test_P5_an_old_link_with_no_terminal_does_not_hide_todays_charge_under_the_same_number(self):
        # Paid weeks ago on the old (test) terminal: no terminal kept. Its number: 41.
        self._old_link_payment('41', paid_at=timezone.now() - timedelta(days=12))
        invoice = self.invoice()
        _opened(invoice, 30)
        # The customer paid on the page; the notify never came. On this terminal the charge is number 41.
        self.day_rows = [row_at('41', '0000041', 25)]
        self.ledger_rows = list(self.day_rows)
        res = self.initiate()
        self.assertEqual(res.status_code, 409)
        self.assertNotIn('iframe_url', res.json(), 'a charge of this sum after the page opened: no second page')
        self.assertEqual(held(invoice)['others'], [('41', 'suspected', '')])

    def test_a_link_with_no_terminal_paid_about_then_still_explains_the_number(self):
        self._old_link_payment('41', paid_at=timezone.now() - timedelta(minutes=25))
        invoice = self.invoice()
        _opened(invoice, 30)
        self.day_rows = [row_at('41', '0000041', 25)]
        self.assertIn('iframe_url', self.initiate().json(), 'the charge paid for that link, not for this order')
        self.assertEqual(held(invoice)['others'], [])

    def test_the_same_rule_guards_a_number_from_paying_twice(self):
        self._old_link_payment('41', paid_at=timezone.now() - timedelta(days=12))
        nobody = '00000000-0000-0000-0000-000000000000'
        now = timezone.now()
        self.assertFalse(_index_paid_for_something_else(nobody, '41', 'iframe_terminal', now))
        self.assertTrue(_index_paid_for_something_else(nobody, '41', 'iframe_terminal', now - timedelta(days=12, hours=3)))
        self.assertTrue(_index_paid_for_something_else(nobody, '41', 'iframe_terminal'), 'no row time: as before')

    def test_a_link_paid_on_this_terminal_explains_the_number_whenever_it_was_paid(self):
        self._old_link_payment('41', paid_at=timezone.now() - timedelta(days=12), terminal='iframe_terminal')
        self.assertTrue(_index_paid_for_something_else(
            '00000000-0000-0000-0000-000000000000', '41', 'iframe_terminal', timezone.now()))


class WebsiteStoreSwitchTest(Base):
    @override_settings(TRANZILA_HOSTED_PAGE_ENABLED=False, STORE_WEBSITE_CARD_PAYMENTS_ENABLED=True)
    def test_the_site_gets_its_page_on_its_own_switch_and_the_till_does_not(self):
        res = self.initiate()
        self.assertEqual(res.status_code, 201)
        self.assertIn('iframe_url', res.json())
        self.assertNotIn('payments_paused', res.json())
        till = PaymentService().initiate_store_purchase(
            [{'product_id': str(self.product.id), 'quantity': 1, 'size': ''}],
            customer_info={'name': 'מזדמן', 'phone': '0500000000'}, callback_url='https://crm.example/cb',
        )
        self.assertEqual((till['success'], till.get('use_direct_card'), till['requires_iframe']), (False, True, False))
        self.assertEqual(StoreInvoice.objects.count(), 1, 'the till wrote nothing')

    @override_settings(TRANZILA_HOSTED_PAGE_ENABLED=True, STORE_WEBSITE_CARD_PAYMENTS_ENABLED=False)
    def test_the_general_switch_does_not_open_the_site(self):
        res = self.initiate()
        self.assertTrue(res.json().get('payments_paused'))
        self.assertEqual(StoreInvoice.objects.count(), 0)


class RefusedRetryStatusTest(Base):
    def test_C1_a_failed_order_reads_pending_while_the_report_cannot_rule_a_payment_out(self):
        invoice = self.invoice()
        _opened(invoice, 45, first_minutes=45)
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, Response='033', index='', ConfirmationCode='')  # the card was declined
        self.assertEqual(self.poll().json()['status'], 'failed')
        self.day_report_down = True
        with self.captureOnCommitCallbacks(execute=True):
            res = self.initiate()
        body = res.json()
        self.assertEqual((res.status_code, body.get('payment_in_review'), body.get('retry_after')), (409, True, 60))
        # Not "failed": nobody can say no charge was made.
        poll = self.poll().json()
        self.assertEqual((poll['status'], poll['payment_reported'], poll['paid']), ('pending', True, False))
        # The report answers again: the poll itself finds that a page may leave.
        self.day_report_down = False
        unpace(invoice)
        poll = self.poll().json()
        self.assertEqual((poll['status'], poll['payment_reported']), ('failed', False))
        self.assertIn('iframe_url', self.initiate().json())

    def test_the_wait_after_a_fresh_page_reads_pending_too_and_ends_by_itself(self):
        invoice = self.invoice(status='failed')
        _opened(invoice, 4, first_minutes=4)
        self.assertEqual(self.initiate().status_code, 409)  # nothing found, four minutes after the page
        self.assertEqual(self.poll().json()['status'], 'pending')
        StoreInvoice.objects.filter(pk=invoice.pk).update(
            payment_page_opened_at=timezone.now() - timedelta(minutes=11),
            payment_page_first_opened_at=timezone.now() - timedelta(minutes=11))
        unpace(invoice)
        self.assertEqual(self.poll().json()['status'], 'failed')
