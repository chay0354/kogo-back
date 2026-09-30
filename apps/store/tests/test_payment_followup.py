"""
Stage 3 — the website store opens on cogolive (CRM half).

A store payment on Tranzila's page that did not end cleanly is followed up
(apps/store/payment_followup.py): a payment the report could not confirm is
asked about again and completed through the notify's own path, a paid order
the website never heard about is told again, and the office hears about what
stays open. Nothing here may charge, refund, or sell the same cart twice.
"""
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.models import OfficeAlert
from apps.core.tranzila_service import TranzilaService
from apps.store import payment_followup
from apps.store.models import StoreInvoice, StoreProduct, StoreProductSize, StoreSale

CALLBACK_URL = '/api/v1/store/payment/callback/'
STATUS_URL = '/api/v1/store/widget/payment/status/'
INITIATE_URL = '/api/v1/store/widget/payment/initiate/'
OLD_ORDER_URL = '/api/v1/store/widget/order/'
KEY = {'HTTP_X_INTEGRATION_KEY': 'test-key'}

SETTINGS = dict(
    WEBSITE_INTEGRATION_API_KEY='test-key',
    WEBSITE_INTEGRATION_URL='https://shop.example',
    TRANZILA_TERMINAL='iframe_terminal',
    TRANZILA_PUBLIC_KEY='iframe_pk',
    TRANZILA_SECRET_KEY='iframe_sk',
    TRANZILA_WEBHOOK_SECRET='',
    TRANZILA_HANDSHAKE_ENABLED=False,
    STORE_WEBSITE_CARD_PAYMENTS_ENABLED=True,
    TRANZILA_HOSTED_PAGE_ENABLED=True,
    CRM_FRONTEND_URL='https://crm.example',
    MANYCHAT_OFFICE_ALERT_FLOW_NS='',
    OFFICE_ALERT_PHONES='',
)


def report_clock(moment):
    local = moment.astimezone(ZoneInfo('Asia/Jerusalem'))
    return {'transaction_date': local.strftime('%Y-%m-%d'), 'transaction_time': local.strftime('%H:%M:%S')}


def paid_row(index='123456', amount='800', approval='0001234'):
    """A report row the way /v1/transactions returns it (cogolive): agorot, Israel time."""
    return {
        'index': index, 'amount': amount, 'processor_response_code': '000', 'tranmode': 'A',
        'authorization_number': approval, **report_clock(timezone.now()),
    }


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)
        self.content = self.text.encode()

    def json(self):
        return self._body


class Site:
    """The website's two endpoints: order-paid (as the test sets it) and stock (always fine)."""

    def __init__(self, paid_status=200):
        self.paid_status = paid_status
        self.paid_calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        if url.endswith('/api/integrations/order-paid'):
            self.paid_calls.append({'body': json, 'timeout': timeout})
            if isinstance(self.paid_status, Exception):
                raise self.paid_status
            return FakeResponse(self.paid_status, {'ok': self.paid_status < 400})
        return FakeResponse(200, {'updated': len((json or {}).get('items') or [])})


def _never(*args, **kwargs):
    raise AssertionError('nothing in the follow-up may move money')


@override_settings(**SETTINGS)
class FollowupBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.product = StoreProduct.objects.create(
            name='חולצה', category='ביגוד', sale_price=Decimal('4.00'), cost_price=Decimal('1.00'),
            stock_quantity=10, website_legacy_id=4399, is_active=True,
        )
        self.site = Site()
        patches = [
            patch('apps.store.website_integration.requests.post', side_effect=self.site.post),
            patch('apps.store.tranzila_store_invoice.issue_store_tranzila_document'),
            patch('apps.store.invoice_email.send_store_invoice_email'),
            # Asserted in every test: the follow-up reads the report, it never charges or refunds.
            patch.object(TranzilaService, 'charge_with_token', side_effect=_never),
            patch.object(TranzilaService, 'charge_with_card', side_effect=_never),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.ledger_rows = []
        self.ledger_down = False
        self.report_calls = []

        def find(service, index):
            self.report_calls.append(str(index))
            if self.ledger_down:
                return {'success': False, 'error': 'timeout'}
            return {'success': True, 'transaction': next((r for r in self.ledger_rows if r['index'] == str(index)), None)}

        ledger = patch.object(TranzilaService, 'find_transaction', autospec=True, side_effect=find)
        ledger.start()
        self.addCleanup(ledger.stop)

    def invoice(self, *, order='CG-260929-AAA1', txn='', code='', status='pending', age=None,
                terminal='iframe_terminal', quantity=2, **extra):
        invoice = StoreInvoice.objects.create(
            customer_name='דנה כהן', customer_phone='0501234567', customer_email='dana@example.com',
            total_amount=Decimal('4.00') * quantity, payment_method='credit_card', payment_status=status,
            website_order_number=order,
            notes=json.dumps([{'product_id': str(self.product.id), 'quantity': quantity, 'size': ''}]),
            tranzila_transaction_id=txn, tranzila_confirmation_code=code,
            tranzila_terminal=terminal if txn else '',
            **extra,
        )
        if age is not None:
            StoreInvoice.objects.filter(pk=invoice.pk).update(created_at=timezone.now() - age)
            invoice.refresh_from_db()
        if txn:
            # A number on the invoice came from a notify: reported when the order was paid.
            StoreInvoice.objects.filter(pk=invoice.pk).update(payment_reported_at=invoice.created_at)
            invoice.refresh_from_db()
        return invoice

    def notify(self, invoice, **overrides):
        payload = {
            'pdesc': invoice.id.hex, 'Response': '000', 'index': '123456',
            'ConfirmationCode': '0001234', 'sum': str(invoice.total_amount), 'currency': '1',
        }
        payload.update(overrides)
        return self.client.post(CALLBACK_URL, payload)

    def state(self, invoice):
        invoice.refresh_from_db()
        self.product.refresh_from_db()
        return (
            invoice.payment_status,
            StoreSale.objects.filter(invoice=invoice).count(),
            self.product.stock_quantity,
        )

    def alerts(self, kind=None):
        qs = OfficeAlert.objects.all()
        return list(qs.filter(kind=kind) if kind else qs)


class RecheckTest(FollowupBase):
    def test_a_payment_the_report_could_not_confirm_is_completed_once_it_can(self):
        invoice = self.invoice()
        self.ledger_down = True
        self.notify(invoice)
        self.assertEqual(self.state(invoice), ('pending', 0, 10))
        self.assertEqual(invoice.tranzila_transaction_id, '123456')

        self.ledger_down = False
        self.ledger_rows = [paid_row()]
        outcome = payment_followup.recheck_pending_payment(invoice.pk, min_interval=timedelta(0))

        self.assertEqual(outcome, payment_followup.RECHECK_COMPLETED)
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        self.assertEqual(invoice.tranzila_terminal, 'iframe_terminal')

    def test_the_report_is_asked_at_most_once_per_interval(self):
        invoice = self.invoice(txn='123456', code='0001234')
        self.assertEqual(payment_followup.recheck_pending_payment(invoice.pk), payment_followup.RECHECK_PENDING)
        self.assertEqual(payment_followup.recheck_pending_payment(invoice.pk), payment_followup.RECHECK_PACED)
        self.assertEqual(self.report_calls, ['123456'])

    def test_a_notify_counts_as_asking(self):
        invoice = self.invoice()
        self.ledger_down = True
        self.notify(invoice)
        self.assertEqual(payment_followup.recheck_pending_payment(invoice.pk), payment_followup.RECHECK_PACED)
        self.assertEqual(len(self.report_calls), 1)

    def test_a_report_that_still_disagrees_sells_nothing(self):
        invoice = self.invoice(txn='123456', code='0001234')
        self.ledger_rows = [paid_row(amount='100')]
        self.assertEqual(payment_followup.recheck_pending_payment(invoice.pk), payment_followup.RECHECK_PENDING)
        self.assertEqual(self.state(invoice), ('pending', 0, 10))

    def test_a_paid_invoice_is_never_sold_again(self):
        invoice = self.invoice(txn='123456', code='0001234')
        self.ledger_rows = [paid_row()]
        payment_followup.recheck_pending_payment(invoice.pk)
        self.assertEqual(
            payment_followup.recheck_pending_payment(invoice.pk, min_interval=timedelta(0)),
            payment_followup.RECHECK_NOT_PENDING,
        )
        self.notify(invoice)  # Tranzila's own notify, late
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

    def test_a_payment_on_another_terminal_is_left_for_the_office(self):
        # The page moved to another terminal since: its report cannot speak for this number.
        invoice = self.invoice(txn='123456', code='0001234', terminal='realtest', age=timedelta(minutes=30))
        with self.captureOnCommitCallbacks(execute=True):
            outcome = payment_followup.recheck_pending_payment(invoice.pk)
        self.assertEqual(outcome, payment_followup.RECHECK_NOT_ELIGIBLE)
        self.assertEqual(self.report_calls, [])
        alert = self.alerts('store_payment_stuck')[0]
        self.assertIn('realtest', alert.why)

    def test_a_till_charge_that_got_no_answer_is_not_touched(self):
        # The till's own "uncertain" invoices: no hosted page, no number reported.
        from apps.core.payment_service import TILL_CHARGE_UNCERTAIN_MARK

        invoice = self.invoice(order=None, code=TILL_CHARGE_UNCERTAIN_MARK)
        self.assertEqual(payment_followup.recheck_pending_payment(invoice.pk), payment_followup.RECHECK_NOT_PENDING)
        self.assertEqual(self.report_calls, [])

    def test_an_invoice_without_its_cart_is_not_completed_empty(self):
        invoice = self.invoice(txn='123456', code='0001234')
        StoreInvoice.objects.filter(pk=invoice.pk).update(notes='Payment failed: 141')
        self.ledger_rows = [paid_row()]
        self.assertEqual(payment_followup.recheck_pending_payment(invoice.pk), payment_followup.RECHECK_NOT_ELIGIBLE)
        self.assertEqual(self.state(invoice), ('pending', 0, 10))

    def test_a_till_walk_in_on_the_hosted_page_is_followed_up_too(self):
        invoice = self.invoice(order=None, txn='123456', code='0001234')
        self.ledger_rows = [paid_row()]
        self.assertEqual(payment_followup.recheck_pending_payment(invoice.pk), payment_followup.RECHECK_COMPLETED)
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        self.assertEqual(self.site.paid_calls, [], 'a till sale has no website to tell')


class StatusEndpointTest(FollowupBase):
    def poll(self, order='CG-260929-AAA1', **headers):
        return self.client.get(STATUS_URL, {'order': order}, **(headers or KEY))

    def test_needs_the_integration_key(self):
        self.invoice()
        self.assertEqual(self.client.get(STATUS_URL, {'order': 'CG-260929-AAA1'}).status_code, 401)
        self.assertEqual(self.poll(HTTP_X_INTEGRATION_KEY='wrong').status_code, 401)

    def test_needs_an_order_that_exists(self):
        self.assertEqual(self.client.get(STATUS_URL, **KEY).status_code, 400)
        self.assertEqual(self.poll('CG-NOPE').status_code, 404)

    def test_an_order_not_paid_yet_is_pending_and_nothing_is_asked(self):
        invoice = self.invoice()
        res = self.poll()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {'status': 'pending', 'invoice_number': invoice.invoice_number, 'paid': False,
                                      'payment_reported': False})
        self.assertEqual(self.report_calls, [])

    def test_a_reported_payment_is_asked_about_again_and_completes(self):
        invoice = self.invoice(txn='123456', code='0001234')
        self.ledger_rows = [paid_row()]
        res = self.poll()
        self.assertEqual(res.json(), {'status': 'completed', 'invoice_number': invoice.invoice_number, 'paid': True,
                                      'payment_reported': False})
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        self.assertIsNotNone(invoice.website_paid_notified_at)

    def test_polling_fast_asks_the_report_once(self):
        self.invoice(txn='123456', code='0001234')
        for _ in range(4):
            self.assertEqual(self.poll().json()['status'], 'pending')
        self.assertEqual(self.report_calls, ['123456'])

    def test_a_failed_order_reads_failed(self):
        invoice = self.invoice(status='failed')
        self.assertEqual(self.poll().json(), {'status': 'failed', 'invoice_number': invoice.invoice_number, 'paid': False,
                                              'payment_reported': False})

    def test_a_paid_order_the_site_missed_is_told_again_from_the_poll(self):
        invoice = self.invoice(status='completed', txn='123456', code='0001234')
        res = self.poll()
        self.assertEqual(res.json()['paid'], True)
        self.assertEqual(len(self.site.paid_calls), 1)
        call = self.site.paid_calls[0]
        self.assertEqual(call['body']['status'], 'paid')
        self.assertEqual(call['body']['website_order_number'], 'CG-260929-AAA1')
        self.assertEqual(call['timeout'], payment_followup.POLL_SITE_TIMEOUT_SECONDS)
        invoice.refresh_from_db()
        self.assertIsNotNone(invoice.website_paid_notified_at)
        self.poll()
        self.assertEqual(len(self.site.paid_calls), 1, 'an acknowledged order is not told again')

    def test_the_poll_answers_even_when_the_follow_up_breaks(self):
        invoice = self.invoice(txn='123456', code='0001234')
        with patch('apps.store.payment_followup.recheck_pending_payment', side_effect=RuntimeError('boom')):
            res = self.poll()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {'status': 'pending', 'invoice_number': invoice.invoice_number, 'paid': False,
                                      'payment_reported': True})

    def test_is_throttled(self):
        from django.conf import settings

        from apps.store.widget_views import WidgetStorePaymentStatusView

        self.assertEqual(WidgetStorePaymentStatusView.throttle_scope, 'store_payment_status')
        self.assertIn('store_payment_status', settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'])


class WebsiteToldTest(FollowupBase):
    def test_the_sites_acknowledgement_is_recorded(self):
        invoice = self.invoice()
        self.ledger_rows = [paid_row()]
        self.notify(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'completed')
        self.assertIsNotNone(invoice.website_paid_notified_at)
        body = self.site.paid_calls[0]['body']
        self.assertEqual((body['status'], body['provider_txn_id'], body['invoice_number']),
                         ('paid', '123456', invoice.invoice_number))

    def test_a_site_that_did_not_answer_is_not_marked_and_not_alerted_before_ten_minutes(self):
        import requests

        invoice = self.invoice()
        self.ledger_rows = [paid_row()]
        self.site.paid_status = requests.ConnectionError('down')
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'completed', 'the sale never waits on the site')
        self.assertIsNone(invoice.website_paid_notified_at)
        self.assertEqual(self.alerts('store_website_not_told'), [])

    def test_the_sweep_tells_it_again_and_the_office_hears_once_after_ten_minutes(self):
        invoice = self.invoice(status='completed', txn='123456', code='0001234', age=timedelta(minutes=11))
        self.site.paid_status = 500
        with self.captureOnCommitCallbacks(execute=True):
            first = payment_followup.sweep_stuck_store_payments()
        self.assertEqual(first['site_not_told'], [invoice])
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_followup_at=None)
        with self.captureOnCommitCallbacks(execute=True):
            payment_followup.sweep_stuck_store_payments()
        self.assertEqual(len(self.site.paid_calls), 2)
        alerts = self.alerts('store_website_not_told')
        self.assertEqual(len(alerts), 1)
        self.assertIn('CG-260929-AAA1', alerts[0].what)
        self.assertIn('HTTP 500', alerts[0].why)

        self.site.paid_status = 200
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_followup_at=None)
        result = payment_followup.sweep_stuck_store_payments()
        self.assertEqual(result['site_told'], [invoice])
        invoice.refresh_from_db()
        self.assertIsNotNone(invoice.website_paid_notified_at)

    def test_an_order_from_the_retired_endpoint_is_never_announced(self):
        # widget/order/ wrote "completed" with no payment and no transaction number.
        self.invoice(status='completed')
        payment_followup.sweep_stuck_store_payments()
        self.assertEqual(self.site.paid_calls, [])


class AlertsTest(FollowupBase):
    def test_a_report_that_disagrees_tells_the_office_at_once_with_the_customer(self):
        invoice = self.invoice()
        self.ledger_rows = [paid_row(amount='100')]
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice)
        alert = self.alerts('store_payment_unverified')[0]
        for expected in ('דנה כהן', '0501234567', 'dana@example.com', '₪8.00', 'CG-260929-AAA1', invoice.invoice_number):
            self.assertIn(expected, alert.customer)
        self.assertIn('123456', alert.what)
        self.assertIn('₪1.00', alert.what, 'what the report showed')
        self.assertIn('iframe_terminal', alert.action)
        self.assertIn('חנות האתר', alert.where)
        self.assertEqual(alert.link, 'https://crm.example/invoices')

    def test_a_report_that_could_not_answer_waits_ten_minutes(self):
        fresh = self.invoice()
        self.ledger_down = True
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(fresh)
        self.assertEqual(self.alerts(), [])

        old = self.invoice(order='CG-260929-OLD1')
        self.notify(old)  # paid, and the report could not answer...
        StoreInvoice.objects.filter(pk=old.pk).update(payment_reported_at=timezone.now() - timedelta(minutes=11))
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(old)  # ...nor eleven minutes later, when Tranzila repeats itself
            self.notify(old)
        alerts = self.alerts('store_payment_stuck')
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].details['invoice_id'], str(old.pk))

    def test_a_forged_notify_without_a_number_alerts_nobody(self):
        invoice = self.invoice()
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='', ConfirmationCode='')
        self.assertEqual(self.alerts(), [])

    def test_every_alert_fits_the_office_template(self):
        from apps.core.office_alerts import _one_line

        invoice = self.invoice(txn='123456', code='0001234', age=timedelta(minutes=30))
        sent = []
        with patch('apps.core.office_alerts.raise_office_alert', side_effect=lambda **kw: sent.append(kw)):
            payment_followup.alert_payment_unverified(invoice, paid_row(amount='100'))
            payment_followup.alert_if_stuck(invoice, why='הדוח של טרנזילה עדיין לא מאשר את העסקה')
            payment_followup.alert_website_not_told(invoice, 'האתר ענה HTTP 500: ' + 'x' * 200)
            payment_followup.alert_oversold(invoice, [
                {'name': 'חולצה עם שם ארוך במיוחד', 'size': 'XL', 'quantity': 3, 'available': 1}] * 2)
            payment_followup.alert_payment_page_failed(invoice, 'Tranzila handshake failed. ' * 5, 'cogolive')
        self.assertEqual(len(sent), 5)
        for alert in sent:
            for name in ('title', 'where', 'what', 'why', 'customer', 'action', 'link'):
                limit = OfficeAlert._meta.get_field(name).max_length
                self.assertLessEqual(
                    len(_one_line(alert.get(name, ''))), limit,
                    f"{alert['kind']}.{name} would be cut: {alert.get(name)}",
                )


@override_settings(STORE_SWEEP_COMPLETES_PAYMENTS=True)
class SweepTest(FollowupBase):
    def test_settles_what_the_report_now_confirms_and_lists_the_rest(self):
        confirmed = self.invoice(order='CG-A', txn='111', code='0001111', age=timedelta(days=1))
        unconfirmed = self.invoice(order='CG-B', txn='222', code='0002222', age=timedelta(days=2))
        # From before this follow-up (no report time) and older than three days:
        # settled by hand, left alone. (A reported one is followed at any age.)
        too_old = self.invoice(order='CG-C', txn='333', code='0003333', age=timedelta(days=4))
        StoreInvoice.objects.filter(pk=too_old.pk).update(payment_reported_at=None)
        never_paid = self.invoice(order='CG-D', age=timedelta(days=1))
        self.ledger_rows = [paid_row(index='111', approval='0001111'), paid_row(index='333', approval='0003333')]

        with self.captureOnCommitCallbacks(execute=True):
            result = payment_followup.sweep_stuck_store_payments()

        self.assertEqual(result['settled'], [confirmed])
        self.assertEqual([inv for inv, _reason in result['still_pending']], [unconfirmed])
        self.assertEqual(sorted(self.report_calls), ['111', '222'])
        for invoice, status in ((confirmed, 'completed'), (unconfirmed, 'pending'), (too_old, 'pending'),
                                (never_paid, 'pending')):
            invoice.refresh_from_db()
            self.assertEqual(invoice.payment_status, status)
        # One alert, for the one still open: the report disagreed, so it went out
        # as "unverified", and the sweep's "stuck" for it is the same event.
        self.assertEqual([(a.kind, a.details['invoice_id']) for a in self.alerts()],
                         [('store_payment_unverified', str(unconfirmed.pk))])

    def test_what_it_does_not_reach_in_time_is_listed_not_dropped(self):
        invoice = self.invoice(txn='111', code='0001111', age=timedelta(hours=1))
        result = payment_followup.sweep_stuck_store_payments(budget_seconds=-1)
        self.assertEqual(result['not_reached'], [invoice])
        self.assertEqual(self.report_calls, [])

    def test_the_morning_brief_item(self):
        from apps.core.daily_brief import GREEN, RED, YELLOW, check_catalogue, run_check

        entry = next(e for e in check_catalogue() if e['key'] == 'stuck_store_payments')
        self.assertEqual(entry['title'], 'תשלומים שנתקעו בחנות')
        self.assertTrue(entry['external'], 'it reads Tranzila and calls the site')

        self.assertEqual(run_check('stuck_store_payments')['severity'], GREEN)

        self.invoice(order='CG-A', txn='111', code='0001111', age=timedelta(hours=2))
        self.ledger_rows = [paid_row(index='111', approval='0001111')]
        settled = run_check('stuck_store_payments')
        self.assertEqual((settled['severity'], settled['count']), (YELLOW, 0))
        self.assertIn('הושלם הבוקר', settled['rows'][0]['detail'])

        self.invoice(order='CG-B', txn='222', code='0002222', age=timedelta(hours=2))
        stuck = run_check('stuck_store_payments')
        self.assertEqual((stuck['severity'], stuck['count']), (RED, 1))
        self.assertIn('CG-B', stuck['rows'][0]['label'])
        self.assertIn('עסקה 222', stuck['rows'][0]['detail'])


class RetiredOrderEndpointTest(FollowupBase):
    def body(self):
        return {
            'website_order_number': 'CG-260929-FREE', 'idempotency_key': 'k-free',
            'customer': {'name': 'x', 'phone': '0500000000'},
            'items': [{'legacy_id': 4399, 'quantity': 2}],
        }

    def test_answers_410_and_writes_nothing(self):
        res = self.client.post(OLD_ORDER_URL, self.body(), format='json', **KEY)
        self.assertEqual(res.status_code, 410)
        self.assertFalse(StoreInvoice.objects.exists())
        self.assertFalse(StoreSale.objects.exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 10)

    def test_answers_410_without_the_key_too(self):
        self.assertEqual(self.client.post(OLD_ORDER_URL, self.body(), format='json').status_code, 410)
        self.assertFalse(StoreInvoice.objects.exists())


class InitiateTest(FollowupBase):
    def payload(self, order='CG-260929-AAA1'):
        return {
            'website_order_number': order, 'idempotency_key': f'idemp-{order}',
            'callback_url': 'https://crm.example/api/v1/store/payment/callback/',
            'success_url': 'https://shop.example/ok', 'error_url': 'https://shop.example/fail',
            'customer': {'name': 'דנה כהן', 'email': 'dana@example.com', 'phone': '0501234567'},
            'items': [{'legacy_id': 4399, 'quantity': 2}],
        }

    def initiate(self, order='CG-260929-AAA1'):
        return self.client.post(INITIATE_URL, self.payload(order), format='json', **KEY)

    # --- the handshake does not answer -------------------------------------

    @override_settings(TRANZILA_HANDSHAKE_ENABLED=True)
    def test_a_handshake_failure_answers_503_and_leaves_nothing_pending(self):
        with patch.object(TranzilaService, 'create_handshake_token', return_value=None), \
                self.captureOnCommitCallbacks(execute=True):
            res = self.initiate()
        self.assertEqual(res.status_code, 503)
        self.assertIn('התשלום אינו זמין כרגע', res.json()['error'])
        invoice = StoreInvoice.objects.get()
        self.assertEqual(invoice.payment_status, 'failed')
        alert = self.alerts('store_page_failed')[0]
        self.assertIn('handshake', alert.why)
        self.assertIn('iframe_terminal', alert.action)

    @override_settings(TRANZILA_HANDSHAKE_ENABLED=True)
    def test_a_handshake_that_raises_is_caught_too(self):
        with patch.object(TranzilaService, 'create_handshake_token', side_effect=ConnectionError('refused')):
            res = self.initiate()
        self.assertEqual(res.status_code, 503)
        self.assertEqual(StoreInvoice.objects.get().payment_status, 'failed')

    @override_settings(TRANZILA_HANDSHAKE_ENABLED=True)
    def test_the_office_hears_once_a_day(self):
        with patch.object(TranzilaService, 'create_handshake_token', return_value=None), \
                self.captureOnCommitCallbacks(execute=True):
            self.initiate('CG-1')
            self.initiate('CG-2')
            self.initiate('CG-1')
        self.assertEqual(len(self.alerts('store_page_failed')), 1)
        self.assertEqual(set(StoreInvoice.objects.values_list('payment_status', flat=True)), {'failed'})

    @override_settings(TRANZILA_HANDSHAKE_ENABLED=True)
    def test_the_site_can_retry_the_same_order_once_tranzila_answers(self):
        with patch.object(TranzilaService, 'create_handshake_token', return_value=None):
            self.initiate()
        with patch.object(TranzilaService, 'create_handshake_token', return_value='thtk-1'):
            res = self.initiate()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn('iframenew.php', res.json()['iframe_url'])
        self.assertEqual(StoreInvoice.objects.count(), 1)
        self.assertEqual(StoreInvoice.objects.get().payment_status, 'pending')

    # --- the order already has a payment the report has not confirmed ------

    def test_a_retry_after_a_confirmed_payment_is_told_it_is_paid(self):
        invoice = self.invoice(txn='123456', code='0001234')
        self.ledger_rows = [paid_row()]
        res = self.initiate()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['already_paid'])
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

    def test_a_retry_while_a_payment_is_unconfirmed_gets_no_second_page(self):
        invoice = self.invoice(txn='123456', code='0001234')
        res = self.initiate()
        self.assertEqual(res.status_code, 409)
        self.assertTrue(res.json()['payment_in_review'])
        self.assertNotIn('iframe_url', res.json())
        self.assertEqual(self.state(invoice), ('pending', 0, 10))


class OversellTest(FollowupBase):
    def test_a_paid_order_beyond_the_shelf_is_kept_marked_and_told(self):
        StoreProduct.objects.filter(pk=self.product.pk).update(stock_quantity=1)
        invoice = self.invoice()
        self.ledger_rows = [paid_row()]
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice)
        self.assertEqual(self.state(invoice), ('completed', 1, -1))
        sale = StoreSale.objects.get(invoice=invoice)
        self.assertEqual(sale.quantity, 2)
        self.assertIn('נמכר מעבר למלאי', sale.notes)
        alert = self.alerts('store_oversold')[0]
        self.assertIn('חולצה', alert.what)
        self.assertIn('הוזמנו 2', alert.what)
        self.assertIn('היו במלאי 1', alert.what)

    def test_a_size_row_that_ran_out_is_flagged_even_though_it_stops_at_zero(self):
        StoreProductSize.objects.create(product=self.product, size='M', stock_quantity=1)
        invoice = self.invoice()
        StoreInvoice.objects.filter(pk=invoice.pk).update(
            notes=json.dumps([{'product_id': str(self.product.id), 'quantity': 2, 'size': 'M'}]),
        )
        self.ledger_rows = [paid_row()]
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'completed')
        self.assertEqual(StoreProductSize.objects.get(product=self.product, size='M').stock_quantity, 0)
        self.assertEqual(len(self.alerts('store_oversold')), 1)

    def test_enough_stock_is_quiet(self):
        invoice = self.invoice()
        self.ledger_rows = [paid_row()]
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice)
        self.assertEqual(StoreSale.objects.get(invoice=invoice).notes, '')
        self.assertEqual(self.alerts('store_oversold'), [])
