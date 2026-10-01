"""
Stage 3, sixth round, part 3: invoices from before this follow-up (probes
S1-S8) — nothing reads them, shows them as "in review", or lets a button
change them — a notify for a till invoice that never had a hosted page (S4),
and the reviewer's sequences across the tools (R2, R3), kept as guards.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from apps.core.tranzila_service import TranzilaService
from apps.store.models import StoreInvoice
from apps.store.serializers import StoreInvoiceSerializer
from apps.store.tests.test_payment_followup import paid_row
from apps.store.tests.test_payment_followup_round4 import _opened
from apps.store.tests.test_payment_followup_round5 import Base, held, second_rows, unpace

OFF = dict(STORE_WEBSITE_CARD_PAYMENTS_ENABLED=False, TRANZILA_HOSTED_PAGE_ENABLED=False,
           STORE_SWEEP_COMPLETES_PAYMENTS=False)


class OldRows(Base):
    def _legacy(self, *, status, txn, terminal, age_days, order, token=False, code=''):
        invoice = StoreInvoice.objects.create(
            customer_name='ישן', total_amount=Decimal('8.00'), payment_method='credit_card',
            payment_status=status, website_order_number=order, tranzila_transaction_id=txn,
            tranzila_confirmation_code=code, tranzila_terminal=terminal, charged_with_token=token,
            notes='[]' if order else 'Payment failed: x',
        )
        StoreInvoice.objects.filter(pk=invoice.pk).update(created_at=timezone.now() - timedelta(days=age_days))
        invoice.refresh_from_db()
        return invoice


@override_settings(**OFF)
class OldRowsAreLeftAloneTest(OldRows):
    def test_S1_old_invoices_are_not_read_written_told_or_shown_in_review(self):
        rows = [
            self._legacy(status='pending', txn='5551', terminal='', age_days=12, order='CG-OLD-1'),
            self._legacy(status='pending', txn='5552', terminal='realtest', age_days=9, order='CG-OLD-2'),
            self._legacy(status='failed', txn='5553', terminal='realtest', age_days=9, order='CG-OLD-3'),
            self._legacy(status='completed', txn='5554', terminal='', age_days=20, order='CG-OLD-4'),
            self._legacy(status='completed', txn='', terminal='', age_days=20, order='CG-OLD-5'),
            self._legacy(status='pending', txn='', terminal='', age_days=1, order='CG-OLD-6'),
            self._legacy(status='failed', txn='', terminal='', age_days=1, order=None),
            self._legacy(status='completed', txn='777', terminal='fxpmichalweb', age_days=1, order=None),
            self._legacy(status='pending', txn='888', terminal='fxpmichalwebtok', age_days=1, order=None,
                         token=True, code='לא ודאי'),
            # From the last three days, on another terminal and on the current one.
            self._legacy(status='pending', txn='5560', terminal='realtest', age_days=2, order='CG-OLD-7'),
            self._legacy(status='pending', txn='5561', terminal='iframe_terminal', age_days=1, order=None),
        ]
        self.ledger_rows = [paid_row(index='5561', approval='0001234')]
        with patch.object(TranzilaService, 'list_all_transactions', side_effect=AssertionError('day report read')):
            result = self.sweep()
        self.assertEqual({k: len(v) for k, v in result.items() if v}, {})
        self.assertEqual(self.report_calls, [])
        self.assertEqual(self.kinds(), [])
        self.assertEqual(self.site.paid_calls, [])
        for row in rows:
            fresh = StoreInvoice.objects.get(pk=row.pk)
            self.assertEqual((fresh.payment_status, fresh.tranzila_transaction_id, fresh.other_transactions,
                              fresh.payment_followup_at, fresh.payment_search_done_at),
                             (row.payment_status, row.tranzila_transaction_id, None, None, None))
            data = StoreInvoiceSerializer(fresh).data
            self.assertEqual((data['payment_in_review'], data['payment_review_numbers']), (False, []))

    def test_S5_initiate_is_paused_and_writes_nothing(self):
        res = self.initiate()
        self.assertTrue(res.json().get('payments_paused'))
        self.assertEqual(StoreInvoice.objects.count(), 0)

    def test_S6_the_status_of_an_old_order_reports_no_payment_and_takes_no_returned_number(self):
        invoice = self._legacy(status='pending', txn='5551', terminal='', age_days=12, order='CG-OLD-1')
        poll = self.poll('CG-OLD-1').json()
        self.assertEqual((poll['status'], poll['payment_reported'], poll['paid']), ('pending', False, False))
        self.assertEqual(self.returned('424242', '0004242', order='CG-OLD-1').status_code, 409)
        self.assertEqual(held(invoice), {'status': 'pending', 'own': '5551', 'code': '', 'others': []})
        self.assertEqual(self.report_calls, [])

    def test_S7_complete_on_an_old_failed_row_changes_nothing(self):
        invoice = self._legacy(status='failed', txn='5551', terminal='realtest', age_days=12, order='CG-OLD-F',
                               code='0005551')
        res = self.review(invoice, 'complete', 'בדיקה')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(held(invoice), {'status': 'failed', 'own': '5551', 'code': '0005551', 'others': []})
        self.assertEqual((self.report_calls, self.kinds(), self.site.paid_calls), ([], [], []))

    def test_S8_release_on_an_old_pending_row_changes_nothing_and_tells_the_site_nothing(self):
        invoice = self._legacy(status='pending', txn='5551', terminal='realtest', age_days=12, order='CG-OLD-P',
                               code='0005551')
        res = self.review(invoice, 'release', 'ישן, realtest')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(held(invoice), {'status': 'pending', 'own': '5551', 'code': '0005551', 'others': []})
        self.assertEqual(self.site.paid_calls, [])


class ManagersToolEligibilityTest(Base):
    def test_complete_on_an_invoice_the_report_can_never_settle_is_refused_untouched(self):
        # Reported under the follow-up, but the hosted page has moved to another terminal since.
        invoice = self.invoice(status='failed', txn='5551', code='0005551', terminal='realtest')
        res = self.review(invoice, 'complete', 'בדיקה')
        self.assertEqual((res.status_code, res.json()['outcome']), (409, 'not_eligible'))
        self.assertIn('מסוף', res.json()['error'])
        self.assertEqual(held(invoice)['status'], 'failed', 'a refused "complete" does not change the status')
        self.assertEqual((self.report_calls, self.site.paid_calls), ([], []))


class TillInvoiceWithoutAPageTest(OldRows):
    def test_S4_a_notify_on_a_paid_till_invoice_that_never_had_a_page_is_only_logged(self):
        invoice = self._legacy(status='completed', txn='777', terminal='fxpmichalweb', age_days=1, order=None)
        with self.captureOnCommitCallbacks(execute=True):
            res = self.notify(invoice, index='31337', ConfirmationCode='0000001')
        self.assertLess(res.status_code, 500)
        self.assertEqual(held(invoice), {'status': 'completed', 'own': '777', 'code': '', 'others': []})
        self.assertEqual(self.kinds(), [], 'no "double charge" for a number the report does not confirm')
        self.assertLessEqual(len(self.report_calls), 1, 'the report is the judge, once — as before the follow-up')
        self.assertEqual({k: len(v) for k, v in self.sweep().items() if v}, {})

    def test_a_second_charge_the_report_confirms_on_such_an_invoice_is_still_recorded(self):
        invoice = self._legacy(status='completed', txn='777', terminal='iframe_terminal', age_days=0, order=None)
        self.ledger_rows = [paid_row(index='31337', approval='0000001')]
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='31337', ConfirmationCode='0000001')
        self.assertEqual(second_rows(invoice).count(), 1)
        self.assertIn('store_second_charge_confirmed', self.kinds())

    def test_an_unconfirmed_notify_on_an_unpaid_till_invoice_without_a_page_keeps_no_number(self):
        invoice = self.invoice(order=None)
        StoreInvoice.objects.filter(pk=invoice.pk).update(website_order_number=None)
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='31337', ConfirmationCode='0000001')
        self.assertEqual(held(invoice), {'status': 'pending', 'own': '', 'code': '', 'others': []})
        self.assertEqual(self.kinds(), [])

    def test_a_till_invoice_whose_page_was_opened_is_followed_as_before(self):
        invoice = self.invoice(order=None)
        StoreInvoice.objects.filter(pk=invoice.pk).update(
            website_order_number=None, payment_page_opened_at=timezone.now() - timedelta(minutes=2))
        self.ledger_down = True
        self.notify(invoice)
        self.assertEqual(held(invoice)['own'], '123456', 'in review')
        self.ledger_down = False
        self.ledger_rows = [paid_row()]
        self.notify(invoice)
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

    @override_settings(TRANZILA_HOSTED_PAGE_ENABLED=True)
    def test_the_tills_secure_page_is_stamped_when_it_is_handed_out(self):
        from apps.core.payment_service import PaymentService

        result = PaymentService().initiate_store_purchase(
            [{'product_id': str(self.product.id), 'quantity': 1, 'size': ''}],
            customer_info={'name': 'מזדמן', 'phone': '0500000000'}, callback_url='https://crm.example/cb',
        )
        self.assertTrue(result['requires_iframe'])
        invoice = StoreInvoice.objects.get(pk=result['invoice_id'])
        self.assertIsNotNone(invoice.payment_page_opened_at)
        self.assertEqual(invoice.payment_page_first_opened_at, invoice.payment_page_opened_at)


class SequenceGuardsTest(Base):
    def test_R2_two_real_numbers_on_an_unpaid_order_one_sale_one_second_charge(self):
        with patch('apps.store.tranzila_store_invoice.issue_store_tranzila_document') as document, \
                patch('apps.store.invoice_email.send_store_invoice_email') as email:
            invoice = self.invoice()
            _opened(invoice, 3)
            self.ledger_down = True
            self.notify(invoice, index='601', ConfirmationCode='0000601')
            self.notify(invoice, index='602', ConfirmationCode='0000602')
            self.ledger_down = False
            self.ledger_rows = [paid_row(index='601', approval='0000601'), paid_row(index='602', approval='0000602')]
            unpace(invoice)
            self.sweep()  # switch off: tells, sells nothing
            self.assertEqual(self.state(invoice), ('pending', 0, 10))
            self.assertEqual(self.review(invoice, 'complete', 'הדוח מאשר').status_code, 200)
            self.assertEqual(self.review(invoice, 'complete', 'שוב').json()['outcome'], 'second_charge')
            self.sweep()
            self.assertEqual(self.state(invoice), ('completed', 1, 8))
            self.assertEqual(second_rows(invoice).count(), 1)
            self.assertEqual((document.call_count, email.call_count), (1, 1))

    def test_R3_a_closed_number_the_report_later_confirms_is_recorded_as_a_second_charge(self):
        invoice = self.invoice()
        _opened(invoice, 3)
        self.ledger_rows = [paid_row()]
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice)
            self.notify(invoice, index='222222', ConfirmationCode='0002222')
        entries = StoreInvoice.objects.get(pk=invoice.pk).other_transactions
        entries[0]['reported_at'] = (timezone.now() - timedelta(minutes=20)).isoformat()
        StoreInvoice.objects.filter(pk=invoice.pk).update(other_transactions=entries)
        self.assertEqual(self.review(invoice, 'close', 'לא מופיע').status_code, 200)
        self.ledger_rows.append(paid_row(index='222222', approval='0002222'))
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='222222', ConfirmationCode='0002222')
        self.assertEqual(second_rows(invoice).count(), 1)
        self.assertIn('store_second_charge_confirmed', self.kinds())
