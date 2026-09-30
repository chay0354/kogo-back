"""
Stage 3, second round — what the independent review of feat/website-store-safety
found (29.9.2026), each as a test that failed on aa7efbb.

The rule behind all of them: an invoice that holds a transaction number
Tranzila reported, and is not completed, is "in review". It is never failed
on the word of a later notify, never offered a second payment page, never
dropped from the follow-up, and no number Tranzila reported for it is lost.
"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.throttling import ScopedRateThrottle

from apps.core.models import OfficeAlert
from apps.core.tranzila_service import TranzilaService
from apps.customers.models import TranzilaTransaction
from apps.store import payment_followup
from apps.store.models import StoreInvoice
from apps.store.tests.test_payment_followup import (
    INITIATE_URL, KEY, STATUS_URL, FollowupBase, paid_row,
)

ORDER = 'CG-260929-AAA1'


def payload(order=ORDER):
    return {
        'website_order_number': order, 'idempotency_key': f'idemp-{order}',
        'callback_url': 'https://crm.example/api/v1/store/payment/callback/',
        'success_url': 'https://shop.example/ok', 'error_url': 'https://shop.example/fail',
        'customer': {'name': 'דנה כהן', 'email': 'dana@example.com', 'phone': '0501234567'},
        'items': [{'legacy_id': 4399, 'quantity': 2}],
    }


class ReviewBase(FollowupBase):
    def setUp(self):
        super().setUp()
        # The day's report, for the page whose notify never came (review item 4).
        self.day_rows = []
        self.day_report_down = False

        def list_all(service, start, end=None, max_pages=20):
            if self.day_report_down:
                return {'success': False, 'error': 'timeout', 'transactions': []}
            return {'success': True, 'transactions': list(self.day_rows)}

        listing = patch.object(TranzilaService, 'list_all_transactions', autospec=True, side_effect=list_all)
        listing.start()
        self.addCleanup(listing.stop)

    def initiate(self, order=ORDER, thtk='thtk-1'):
        with patch.object(TranzilaService, 'create_handshake_token', return_value=thtk):
            return self.client.post(INITIATE_URL, payload(order), format='json', **KEY)

    def poll(self, order=ORDER):
        return self.client.get(STATUS_URL, {'order': order}, **KEY)

    def kinds(self):
        return [a.kind for a in OfficeAlert.objects.order_by('created_at')]


# ---------------------------------------------------------------------------
# Review 1 — a reported, unfinished payment is in review, never failed
# ---------------------------------------------------------------------------

class ReportedPaymentIsInReviewTest(ReviewBase):
    def test_A_a_late_decline_does_not_bury_a_reported_payment(self):
        invoice = self.invoice()
        self.ledger_down = True
        self.notify(invoice)  # tab 1 approved; the report cannot answer
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, Response='033', index='', ConfirmationCode='')  # tab 2 declined
        invoice.refresh_from_db()
        self.assertEqual((invoice.payment_status, invoice.tranzila_transaction_id), ('pending', '123456'))
        self.assertIn('store_payment_conflict', self.kinds())
        self.assertEqual(self.site.paid_calls, [], 'the site is not told "failed" either')

        self.ledger_down = False
        res = self.poll()
        self.assertEqual(res.json(), {
            'status': 'pending', 'invoice_number': invoice.invoice_number, 'paid': False, 'payment_reported': True,
        })
        res = self.initiate()
        self.assertEqual(res.status_code, 409)
        self.assertTrue(res.json()['payment_in_review'])
        self.assertNotIn('iframe_url', res.json())

    def test_a_late_decline_completes_the_order_when_the_report_confirms_it(self):
        invoice = self.invoice()
        self.ledger_down = True
        self.notify(invoice)
        self.ledger_down = False
        self.ledger_rows = [paid_row()]
        self.notify(invoice, Response='033', index='', ConfirmationCode='')
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

    def test_a_late_decline_fails_the_order_only_when_the_report_says_no(self):
        invoice = self.invoice()
        self.ledger_down = True
        self.notify(invoice)
        self.ledger_down = False
        self.ledger_rows = [{**paid_row(), 'processor_response_code': '033'}]  # the report: declined
        self.notify(invoice, Response='033', index='', ConfirmationCode='')
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'failed')
        self.assertEqual(self.poll().json()['payment_reported'], False)
        self.assertEqual(self.poll().json()['status'], 'failed')
        entry = invoice.other_transactions[0]
        self.assertEqual((entry['index'], entry['state']), ('123456', 'rejected'), 'the number is kept, marked')

    @override_settings(TRANZILA_HANDSHAKE_ENABLED=True)
    def test_B_an_approved_notify_on_a_failed_order_brings_it_back_to_review(self):
        invoice = self.invoice()
        StoreInvoice.objects.filter(pk=invoice.pk).update(website_idempotency_key=f'idemp-{ORDER}')
        with patch.object(TranzilaService, 'create_handshake_token', return_value=None):
            self.client.post(INITIATE_URL, payload(), format='json', **KEY)  # a retry's page failed
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'failed')

        self.ledger_down = True
        self.notify(invoice)  # but the first page was paid; the report cannot answer yet
        invoice.refresh_from_db()
        self.assertEqual((invoice.payment_status, invoice.tranzila_transaction_id), ('pending', '123456'))
        self.assertTrue(self.poll().json()['payment_reported'])

        self.ledger_down = False
        self.ledger_rows = [paid_row()]
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_followup_at=None)
        res = self.poll()
        self.assertEqual((res.json()['status'], res.json()['paid']), ('completed', True))
        self.assertTrue(self.initiate().json()['already_paid'])

    @override_settings(TRANZILA_HANDSHAKE_ENABLED=True)
    def test_the_503_path_never_fails_an_order_a_payment_was_reported_for(self):
        invoice = self.invoice(status='failed')
        StoreInvoice.objects.filter(pk=invoice.pk).update(website_idempotency_key=f'idemp-{ORDER}')

        def notify_lands_meanwhile(*args, **kwargs):
            # The first page's notify arrives while the retry asks for a new page.
            StoreInvoice.objects.filter(pk=invoice.pk).update(
                tranzila_transaction_id='123456', tranzila_confirmation_code='0001234',
                tranzila_terminal='iframe_terminal',
            )
            return None

        with patch.object(TranzilaService, 'create_handshake_token', side_effect=notify_lands_meanwhile):
            res = self.client.post(INITIATE_URL, payload(), format='json', **KEY)
        self.assertEqual(res.status_code, 503)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'pending')

    def test_a_reported_payment_older_than_three_days_stays_in_the_brief(self):
        from apps.core.daily_brief import run_check

        invoice = self.invoice(txn='123456', code='0001234', age=timedelta(days=4))
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_reported_at=timezone.now() - timedelta(days=4))
        item = run_check('stuck_store_payments')
        self.assertEqual(item['count'], 1)
        self.assertIn(ORDER, item['rows'][0]['label'])


# ---------------------------------------------------------------------------
# Review 2 — the website's contract
# ---------------------------------------------------------------------------

class WebsiteContractTest(ReviewBase):
    def test_a_refunded_order_is_refunded_and_not_paid(self):
        for status in ('refunded', 'refund_failed'):
            StoreInvoice.objects.all().delete()
            invoice = self.invoice(status=status, txn='123456', code='0001234')
            self.assertEqual(self.poll().json(), {
                'status': 'refunded', 'invoice_number': invoice.invoice_number, 'paid': False,
                'payment_reported': False,
            })

    def test_an_order_from_the_retired_endpoint_is_not_paid(self):
        # widget/order/ wrote "completed" with no payment and no transaction number.
        invoice = self.invoice(status='completed')
        res = self.poll().json()
        self.assertEqual(res['paid'], False)
        self.assertNotEqual(res['status'], 'completed')
        self.assertEqual(res['invoice_number'], invoice.invoice_number)

    def test_a_paid_order_is_completed_and_paid(self):
        invoice = self.invoice(status='completed', txn='123456', code='0001234')
        self.assertEqual(self.poll().json(), {
            'status': 'completed', 'invoice_number': invoice.invoice_number, 'paid': True, 'payment_reported': False,
        })

    def test_an_unpaid_order_reports_nothing(self):
        invoice = self.invoice()
        self.assertEqual(self.poll().json(), {
            'status': 'pending', 'invoice_number': invoice.invoice_number, 'paid': False, 'payment_reported': False,
        })


# ---------------------------------------------------------------------------
# Review 3 — no reported number is lost
# ---------------------------------------------------------------------------

class NoNumberIsLostTest(ReviewBase):
    @override_settings(STORE_SWEEP_COMPLETES_PAYMENTS=True)
    def test_C_the_first_tabs_number_survives_the_second_tabs_payment(self):
        invoice = self.invoice()
        self.ledger_down = True
        self.notify(invoice, index='111111', ConfirmationCode='0001111')  # tab 1, report down
        self.ledger_down = False
        self.ledger_rows = [paid_row(index='222222', approval='0002222')]
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='222222', ConfirmationCode='0002222')  # tab 2, confirmed
        invoice.refresh_from_db()
        self.assertEqual((invoice.payment_status, invoice.tranzila_transaction_id), ('completed', '222222'))
        self.assertEqual([(e['index'], e['state']) for e in invoice.other_transactions], [('111111', 'open')])
        alert = OfficeAlert.objects.get(kind='store_possible_double_charge')
        self.assertIn('111111', alert.what)
        self.assertIn('222222', alert.what)

        # Tab 1's charge shows up in the report by the morning: a real second
        # charge, for a refund.
        self.ledger_rows.append(paid_row(index='111111', approval='0001111'))
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_followup_at=None)
        payment_followup.sweep_stuck_store_payments()
        invoice.refresh_from_db()
        self.assertEqual([(e['index'], e['state']) for e in invoice.other_transactions], [('111111', 'second_charge')])
        self.assertTrue(TranzilaTransaction.objects.filter(
            idempotency_key=f'store_second_{invoice.id}_111111', transaction_id='111111').exists())

    def test_E_a_second_charge_reported_while_the_report_is_down_is_kept(self):
        invoice = self.invoice()
        self.ledger_rows = [paid_row(index='111111', approval='0001111')]
        self.notify(invoice, index='111111', ConfirmationCode='0001111')  # page 1, confirmed
        self.ledger_down = True
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='222222', ConfirmationCode='0002222')  # page 2 paid too
        invoice.refresh_from_db()
        self.assertEqual([(e['index'], e['state']) for e in invoice.other_transactions], [('222222', 'open')])
        alert = OfficeAlert.objects.get(kind='store_possible_double_charge')
        self.assertIn('111111', alert.what)
        self.assertIn('222222', alert.what)

        self.ledger_down = False
        self.ledger_rows.append(paid_row(index='222222', approval='0002222'))
        payment_followup.sweep_stuck_store_payments()
        self.assertTrue(TranzilaTransaction.objects.filter(
            idempotency_key=f'store_second_{invoice.id}_222222').exists())
        self.assertEqual(self.state(invoice), ('completed', 1, 8), 'sold once')


# ---------------------------------------------------------------------------
# Review 4 (owner's decision pending) — the morning sweep does not sell by default
# ---------------------------------------------------------------------------

class SweepSwitchTest(ReviewBase):
    def test_D_by_default_the_sweep_only_checks_and_tells_the_office(self):
        invoice = self.invoice(txn='123456', code='0001234', age=timedelta(hours=5))
        self.ledger_rows = [paid_row()]
        with patch('apps.store.tranzila_store_invoice.issue_store_tranzila_document') as doc, \
                patch('apps.store.invoice_email.send_store_invoice_email') as mail, \
                self.captureOnCommitCallbacks(execute=True):
            result = payment_followup.sweep_stuck_store_payments()
        self.assertEqual(result['settled'], [])
        self.assertEqual(result['confirmed'], [invoice])
        self.assertEqual((doc.call_count, mail.call_count, len(self.site.paid_calls)), (0, 0, 0))
        self.assertEqual(self.state(invoice), ('pending', 0, 10))
        self.assertIn('store_payment_confirmed', self.kinds())

    @override_settings(STORE_SWEEP_COMPLETES_PAYMENTS=True)
    def test_with_the_switch_on_the_sweep_completes(self):
        invoice = self.invoice(txn='123456', code='0001234', age=timedelta(hours=5))
        self.ledger_rows = [paid_row()]
        result = payment_followup.sweep_stuck_store_payments()
        self.assertEqual(result['settled'], [invoice])
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

    def test_the_customers_own_poll_still_completes(self):
        invoice = self.invoice(txn='123456', code='0001234', age=timedelta(hours=5))
        self.ledger_rows = [paid_row()]
        self.assertTrue(self.poll().json()['paid'])
        self.assertEqual(self.state(invoice), ('completed', 1, 8))


# ---------------------------------------------------------------------------
# Review 4 — a notify that never came
# ---------------------------------------------------------------------------

class NotifyNeverCameTest(ReviewBase):
    def charge_row(self, **extra):
        row = {**paid_row(index='777777', approval='0007777'), 'amount': '800'}
        row.update(extra)
        return row

    def opened_page(self, minutes_ago=5):
        invoice = self.invoice()
        StoreInvoice.objects.filter(pk=invoice.pk).update(
            website_idempotency_key=f'idemp-{ORDER}',
            payment_page_opened_at=timezone.now() - timedelta(minutes=minutes_ago),
        )
        return invoice

    def test_a_matching_charge_on_the_report_blocks_a_second_page(self):
        self.opened_page()
        self.day_rows = [self.charge_row()]
        with self.captureOnCommitCallbacks(execute=True):
            res = self.initiate()
        self.assertEqual(res.status_code, 409)
        self.assertTrue(res.json()['payment_in_review'])
        alert = OfficeAlert.objects.get(kind='store_payment_unreported')
        self.assertIn('777777', alert.what)

    def test_a_report_that_cannot_answer_blocks_it_too(self):
        self.opened_page()
        self.day_report_down = True
        self.assertEqual(self.initiate().status_code, 409)

    def test_nothing_on_the_report_opens_the_page(self):
        self.opened_page()
        self.day_rows = [
            self.charge_row(amount='900'),                           # another sum
            self.charge_row(processor_response_code='033'),          # declined
            self.charge_row(tranmode='N'),                           # a card check, not a charge
        ]
        res = self.initiate()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn('iframe_url', res.json())

    def test_a_charge_that_belongs_to_another_order_does_not_block(self):
        self.opened_page()
        StoreInvoice.objects.create(
            total_amount=8, payment_method='credit_card', payment_status='completed',
            tranzila_transaction_id='777777', tranzila_terminal='iframe_terminal',
        )
        self.day_rows = [self.charge_row()]
        self.assertEqual(self.initiate().status_code, 200)

    def test_a_page_opened_long_ago_is_not_searched(self):
        self.opened_page(minutes_ago=45)
        self.day_report_down = True
        self.assertEqual(self.initiate().status_code, 200)


# ---------------------------------------------------------------------------
# Review 6-9 — noise, time, the token, the key
# ---------------------------------------------------------------------------

class NoiseAndTimeTest(ReviewBase):
    def test_not_in_the_report_right_after_paying_is_not_an_alarm_yet(self):
        invoice = self.invoice()
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice)  # the report does not list it yet
        self.assertEqual(self.kinds(), [])
        StoreInvoice.objects.filter(pk=invoice.pk).update(
            payment_reported_at=timezone.now() - timedelta(minutes=11), payment_followup_at=None)
        with self.captureOnCommitCallbacks(execute=True):
            payment_followup.sweep_stuck_store_payments()
        alerts = list(OfficeAlert.objects.all())
        self.assertEqual(len(alerts), 1, 'ten minutes after the payment it is')
        self.assertIn('לא נמצאה בדוח', alerts[0].why)

    def test_the_ten_minutes_count_from_the_payment_not_the_order(self):
        invoice = self.invoice(age=timedelta(minutes=30))  # the order was opened half an hour ago
        self.ledger_down = True
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice)  # and paid now
        self.assertEqual(self.kinds(), [])

    def test_a_completion_from_the_poll_tells_the_site_on_a_short_leash(self):
        self.invoice(txn='123456', code='0001234')
        self.ledger_rows = [paid_row()]
        self.poll()
        self.assertEqual([c['timeout'] for c in self.site.paid_calls], [payment_followup.POLL_SITE_TIMEOUT_SECONDS])

    def test_a_token_is_never_kept_or_shown_as_a_transaction_number(self):
        invoice = self.invoice(age=timedelta(minutes=30))
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post('/api/v1/store/payment/callback/', {
                'pdesc': invoice.id.hex, 'Response': '000', 'TranzilaTK': 'Z1234secret5678',
                'ConfirmationCode': '0001234', 'sum': '8.00',
            })
            payment_followup.sweep_stuck_store_payments()
        invoice.refresh_from_db()
        self.assertEqual(invoice.tranzila_transaction_id, '')
        self.assertFalse(any('Z1234secret5678' in json.dumps(a.__dict__, default=str, ensure_ascii=False)
                             for a in OfficeAlert.objects.all()))


class KeyBeforeThrottleTest(ReviewBase):
    def test_callers_without_the_key_do_not_use_up_the_shops_rate(self):
        self.invoice()
        cache.clear()
        with patch.dict(ScopedRateThrottle.THROTTLE_RATES, {'store_payment_status': '2/min'}):
            for _ in range(3):
                self.assertEqual(self.client.get(STATUS_URL, {'order': ORDER}).status_code, 401)
            self.assertEqual(self.poll().status_code, 200)
        cache.clear()

    def test_the_key_is_compared_in_constant_time(self):
        import hmac

        self.invoice()
        with patch('apps.store.widget_views.hmac.compare_digest', wraps=hmac.compare_digest) as compare:
            self.poll()
        compare.assert_called()


class SideGuardsTest(ReviewBase):
    def test_a_refunded_order_is_not_offered_a_new_page(self):
        self.invoice(status='refunded', txn='123456', code='0001234')
        res = self.initiate()
        self.assertEqual(res.status_code, 400)
        self.assertNotIn('iframe_url', res.json())

    def test_a_second_unconfirmed_number_sits_beside_the_first(self):
        invoice = self.invoice()
        self.ledger_down = True
        self.notify(invoice, index='111111', ConfirmationCode='0001111')
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='222222', ConfirmationCode='0002222')
        invoice.refresh_from_db()
        self.assertEqual(invoice.tranzila_transaction_id, '111111', 'never written over')
        self.assertEqual([(e['index'], e['state']) for e in invoice.other_transactions], [('222222', 'open')])
        self.assertEqual(self.kinds(), ['store_possible_double_charge'])

    def test_a_number_the_report_ruled_out_is_not_reopened_by_a_repeat(self):
        invoice = self.invoice()
        self.ledger_down = True
        self.notify(invoice)
        self.ledger_down = False
        self.ledger_rows = [{**paid_row(), 'processor_response_code': '033'}]
        self.notify(invoice, Response='033', index='', ConfirmationCode='')  # the report says no: failed
        self.notify(invoice)  # Tranzila repeats the old approved notify
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'failed')
        self.assertFalse(self.poll().json()['payment_reported'])

    def test_the_report_is_read_outside_the_row_lock(self):
        from django.db import connection

        invoice = self.invoice()
        depth = len(connection.savepoint_ids)
        seen = []
        from apps.payment_links import public_views
        original = public_views.verify_transaction_with_tranzila

        def verify(*args, **kwargs):
            seen.append(len(connection.savepoint_ids))
            return original(*args, **kwargs)

        with patch('apps.payment_links.public_views.verify_transaction_with_tranzila', side_effect=verify):
            self.notify(invoice)
            payment_followup.recheck_pending_payment(invoice.pk, min_interval=timedelta(0))
        self.assertTrue(seen)
        self.assertEqual(set(seen), {depth}, 'no transaction of ours is open while Tranzila is asked')

    def test_an_unknown_order_id_is_not_found_rather_than_an_error(self):
        res = self.client.post('/api/v1/store/payment/callback/', {'pdesc': 'not-an-id', 'Response': '000', 'index': '1'})
        self.assertFalse(res.data['success'])
