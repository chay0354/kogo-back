"""
Stage 3, third round — what the second review of feat/website-store-safety
found (30.9.2026): the reviewer's probes (reviewtests2/test_probes.py), each
turned into a test of the safe behaviour, that failed before this round.

The money rules they hold:
  * a retry never writes over a sale, and a cart is never sold twice;
  * a "declined" fails an order only when every number open at that moment
    was asked about, and only on the report's definite "no";
  * a payment whose notify never came is looked for, kept and shown as
    reported, and blocks a second page until a person releases it;
  * the morning sweep with its switch off reads and tells — it writes nothing.
"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import OfficeAlert, UserProfile
from apps.core.tranzila_service import TranzilaService
from apps.customers.models import TranzilaTransaction
from apps.store import payment_followup
from apps.store.models import StoreInvoice, StoreSale
from apps.store.tests.test_payment_followup import KEY, paid_row
from apps.store.tests.test_payment_followup_review import ORDER, ReviewBase

RETURNED_URL = '/api/v1/store/widget/payment/returned/'


def _opened(invoice, minutes):
    StoreInvoice.objects.filter(pk=invoice.pk).update(
        website_idempotency_key=f'idemp-{ORDER}',
        payment_page_opened_at=timezone.now() - timedelta(minutes=minutes),
    )
    invoice.refresh_from_db()


def _manager_client(role=UserProfile.ROLE_MANAGER, email='manager@example.com'):
    user = get_user_model().objects.create_user(username=email, email=email, password='x12345678!')
    UserProfile.objects.update_or_create(user=user, defaults={'role': role})
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
    return client, user


# ---------------------------------------------------------------------------
# P1 — a retry never writes over a sale, and a cart is never sold twice
# ---------------------------------------------------------------------------

class InitiateRaceTest(ReviewBase):
    def test_P1a_a_sale_completed_meanwhile_is_not_reopened_or_sold_again(self):
        invoice = self.invoice(status='failed')  # tab A was declined
        _opened(invoice, 5)
        self.ledger_rows = [paid_row()]  # tab B's payment 123456 is real

        def during(inv):
            self.notify(invoice)  # tab B's notify lands while initiate reads the day report
            return ('none', [])

        with patch('apps.store.payment_followup.find_unreported_payment', side_effect=during):
            res = self.initiate()
        self.assertNotIn('iframe_url', res.json(), 'no second page for a paid order')
        self.assertTrue(res.json().get('already_paid'))
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

        # Even if a second payment is reported anyway, the cart is not sold again.
        self.ledger_rows = [paid_row(), paid_row(index='654321', approval='0006543')]
        self.notify(invoice, index='654321', ConfirmationCode='0006543')
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        self.assertTrue(TranzilaTransaction.objects.filter(
            idempotency_key=f'store_second_{invoice.id}_654321').exists())

    def test_P1b_a_payment_reported_meanwhile_gets_no_second_page(self):
        invoice = self.invoice(status='failed')
        _opened(invoice, 5)
        self.ledger_down = True

        def during(inv):
            self.notify(invoice)  # tab B approved -> in review
            return ('none', [])

        with patch('apps.store.payment_followup.find_unreported_payment', side_effect=during):
            res = self.initiate()
        self.assertEqual(res.status_code, 409)
        self.assertTrue(res.json()['payment_in_review'])
        self.assertNotIn('iframe_url', res.json())

    def test_P1c_one_payment_one_sale(self):
        invoice = self.invoice(status='failed')
        _opened(invoice, 5)
        self.ledger_rows = [paid_row()]

        def during(inv):
            self.notify(invoice)
            return ('none', [])

        with patch('apps.store.payment_followup.find_unreported_payment', side_effect=during):
            self.initiate()
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_followup_at=None)
        self.poll()
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

    def test_a_sold_invoice_is_never_sold_again_whatever_its_status_reads(self):
        # The guard in the sale itself: an invoice that already has its sale
        # lines (a status written over by anything) is not sold a second time.
        invoice = self.invoice()
        self.ledger_rows = [paid_row()]
        self.notify(invoice)
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_status='pending', payment_followup_at=None)
        self.poll()
        self.notify(invoice)
        self.assertEqual(self.state(invoice), ('completed', 1, 8))


# ---------------------------------------------------------------------------
# P2, P8 — the "declined" race, and each number's own clock
# ---------------------------------------------------------------------------

class DeclineRaceTest(ReviewBase):
    def test_P2_a_number_reported_during_the_decline_read_keeps_the_order_in_review(self):
        invoice = self.invoice(txn='111111', code='0001111', age=timedelta(minutes=15))
        StoreInvoice.objects.filter(pk=invoice.pk).update(website_idempotency_key=f'idemp-{ORDER}')
        fired = {'y': False}

        def find(service, index):
            index = str(index)
            if index == '111111' and not fired['y']:
                fired['y'] = True
                self.notify(invoice, index='222222', ConfirmationCode='0002222')  # tab B, mid-read
                return {'success': True, 'transaction': None}
            if index == '222222' and not self.ledger_rows:
                return {'success': False, 'error': 'timeout'}
            return {'success': True, 'transaction': next((r for r in self.ledger_rows if r['index'] == index), None)}

        with patch.object(TranzilaService, 'find_transaction', new=lambda service, index: find(service, index)):
            with self.captureOnCommitCallbacks(execute=True):
                self.notify(invoice, Response='033', index='', ConfirmationCode='')
            invoice.refresh_from_db()
            self.assertNotEqual(invoice.payment_status, 'failed')
            self.assertTrue(payment_followup.holds_reported_payment(invoice))
            self.assertNotIn('failed', [c['body']['status'] for c in self.site.paid_calls])

            # Later the report confirms 222222: the customer's poll completes it.
            self.ledger_rows = [paid_row(index='222222', approval='0002222')]
            StoreInvoice.objects.filter(pk=invoice.pk).update(payment_followup_at=None)
            self.assertTrue(self.poll().json()['paid'])
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

    def test_P8_a_fresh_number_is_judged_by_its_own_clock(self):
        invoice = self.invoice(txn='111111', code='0001111', age=timedelta(minutes=30))
        StoreInvoice.objects.filter(pk=invoice.pk).update(website_idempotency_key=f'idemp-{ORDER}')
        self.ledger_rows = [{**paid_row(index='111111', approval='0001111'), 'processor_response_code': '033'}]
        self.notify(invoice, Response='033', index='', ConfirmationCode='')  # the report says no -> failed
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'failed')
        self.assertIn('iframe_url', self.initiate().json())  # retry: a new page

        before = timezone.now()
        self.notify(invoice, index='222222', ConfirmationCode='0002222')  # paid; not listed yet
        invoice.refresh_from_db()
        self.assertGreaterEqual(invoice.payment_reported_at, before, 'the new number has its own clock')
        self.notify(invoice, Response='033', index='', ConfirmationCode='')  # another tab declines
        invoice.refresh_from_db()
        self.assertEqual((invoice.payment_status, invoice.tranzila_transaction_id), ('pending', '222222'))
        self.assertNotIn(('222222', 'rejected'), [(e['index'], e['state']) for e in invoice.other_transactions or []])

    def test_only_a_definite_no_fails_on_a_decline(self):
        # A different approval number, or a number the report never lists, is
        # not a "no": the order stays in review for a person.
        cases = {
            'approval': [{**paid_row(), 'authorization_number': '0009999'}],
            'missing': [],
        }
        for name, rows in cases.items():
            StoreInvoice.objects.all().delete()
            invoice = self.invoice(txn='123456', code='0001234', age=timedelta(hours=1))
            self.ledger_rows = rows
            self.notify(invoice, Response='033', index='', ConfirmationCode='')
            invoice.refresh_from_db()
            self.assertEqual(invoice.payment_status, 'pending', name)
        for name, row in {
            'declined': {**paid_row(), 'processor_response_code': '033'},
            'not a charge': {**paid_row(), 'tranmode': 'N'},
            'another sum': {**paid_row(), 'amount': '100'},
        }.items():
            StoreInvoice.objects.all().delete()
            invoice = self.invoice(txn='123456', code='0001234', age=timedelta(hours=1))
            self.ledger_rows = [row]
            self.notify(invoice, Response='033', index='', ConfirmationCode='')
            invoice.refresh_from_db()
            self.assertEqual(invoice.payment_status, 'failed', name)


# ---------------------------------------------------------------------------
# P3, P4 — a payment whose notify never came
# ---------------------------------------------------------------------------

class LostNotifyTest(ReviewBase):
    def test_P3_the_sweep_finds_it_and_it_blocks_a_second_page_past_thirty_minutes(self):
        invoice = self.invoice()
        _opened(invoice, 45)
        self.day_rows = [paid_row(index='555555')]
        with self.captureOnCommitCallbacks(execute=True):
            sweep = payment_followup.sweep_stuck_store_payments(complete=True)
        self.assertEqual(sweep['unexplained'], [invoice])
        alert = OfficeAlert.objects.get(kind='store_payment_unreported')
        self.assertIn('555555', alert.what)
        self.assertEqual(self.state(invoice), ('pending', 0, 10), 'listed and told, never completed')
        self.assertTrue(self.poll().json()['payment_reported'])
        res = self.initiate()
        self.assertEqual(res.status_code, 409)
        self.assertNotIn('iframe_url', res.json())

    def test_with_the_switch_off_the_sweep_keeps_and_tells_a_lost_payment_and_sells_nothing(self):
        invoice = self.invoice()
        _opened(invoice, 45)
        self.day_rows = [paid_row(index='555555')]
        with self.captureOnCommitCallbacks(execute=True):
            sweep = payment_followup.sweep_stuck_store_payments()
        self.assertEqual(sweep['unexplained'], [invoice])
        self.assertTrue(OfficeAlert.objects.filter(kind='store_payment_unreported').exists())
        # Kept on the invoice as suspected (round 5: keeping it is not a sale,
        # a document or an email) — the order is in review at once.
        self.assertEqual(self.state(invoice), ('pending', 0, 10))
        self.assertEqual([(e['index'], e['state']) for e in invoice.other_transactions], [('555555', 'suspected')])
        self.assertTrue(self.poll().json()['payment_reported'])
        self.assertEqual(self.initiate().status_code, 409)

    def test_P4_the_409_from_the_day_report_shows_in_the_status(self):
        invoice = self.invoice(status='failed')
        _opened(invoice, 5)
        self.day_rows = [paid_row(index='777777')]
        self.assertEqual(self.initiate().status_code, 409)
        status = self.poll().json()
        self.assertEqual((status['status'], status['payment_reported'], status['paid']), ('pending', True, False))

    def test_a_suspected_charge_is_never_completed_by_itself(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        self.day_rows = [paid_row(index='555555')]
        self.ledger_rows = [paid_row(index='555555')]  # the report would confirm it by number
        self.initiate()
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_followup_at=None)
        self.poll()
        payment_followup.sweep_stuck_store_payments(complete=True)
        self.assertEqual(self.state(invoice), ('pending', 0, 10), 'the terminal is shared: a person decides')

    def test_the_returned_number_is_reported_like_a_notify(self):
        invoice = self.invoice()
        _opened(invoice, 5)  # round 4: taken only for an order whose page was opened recently
        self.ledger_down = True
        res = self.client.post(RETURNED_URL, {'order': ORDER, 'index': '123456'}, format='json', **KEY)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {
            'status': 'pending', 'invoice_number': invoice.invoice_number, 'paid': False, 'payment_reported': True,
        })
        invoice.refresh_from_db()
        self.assertEqual(invoice.tranzila_transaction_id, '123456')

        # With the approval code the report can tie it to the order: completed.
        self.ledger_down = False
        self.ledger_rows = [paid_row()]
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_followup_at=None)
        res = self.client.post(RETURNED_URL, {'order': ORDER, 'index': '123456', 'code': '0001234'}, format='json', **KEY)
        self.assertEqual(res.json()['paid'], True)
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

    def test_a_returned_number_of_another_order_is_kept_and_does_not_pass(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        self.ledger_rows = [paid_row(amount='5000')]  # another sum: not this order's payment
        res = self.client.post(RETURNED_URL, {'order': ORDER, 'index': '123456', 'code': '0001234'}, format='json', **KEY)
        self.assertEqual(res.json()['paid'], False)
        self.assertEqual(self.state(invoice), ('pending', 0, 10))
        invoice.refresh_from_db()
        self.assertEqual(invoice.tranzila_transaction_id, '123456', 'kept for the office')

    def test_the_returned_endpoint_answers_like_the_others(self):
        self.invoice()
        self.assertEqual(self.client.post(RETURNED_URL, {'order': ORDER, 'index': '1'}, format='json').status_code, 401)
        self.assertEqual(self.client.post(RETURNED_URL, {'order': 'NOPE', 'index': '1'}, format='json', **KEY).status_code, 404)
        self.assertEqual(self.client.post(RETURNED_URL, {'order': ORDER, 'index': 'Z1token'}, format='json', **KEY).status_code, 400)


# ---------------------------------------------------------------------------
# P5 — a person releases an order in review
# ---------------------------------------------------------------------------

class ReleaseToolTest(ReviewBase):
    def url(self, invoice):
        return f'/api/v1/store/invoices/{invoice.pk}/payment-review/'

    def in_review(self):
        invoice = self.invoice()
        StoreInvoice.objects.filter(pk=invoice.pk).update(website_idempotency_key=f'idemp-{ORDER}')
        self.notify(invoice, index='999999', ConfirmationCode='0009999')  # never in the report
        invoice.refresh_from_db()
        return invoice

    def test_P5_a_person_releases_an_order_the_report_never_confirms(self):
        invoice = self.in_review()
        self.assertEqual(self.initiate().status_code, 409)
        manager, user = _manager_client()
        res = manager.post(self.url(invoice), {'action': 'release', 'reason': 'נבדק בטרנזילה: אין עסקה כזאת'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'failed')
        self.assertFalse(self.poll().json()['payment_reported'])
        self.assertIn('iframe_url', self.initiate().json())
        entry = next(e for e in invoice.other_transactions if e['index'] == '999999')
        # Round 4: released, not ruled out — still asked about (test_payment_followup_round4).
        self.assertEqual(entry['state'], 'released')
        self.assertEqual(entry['release_reason'], 'נבדק בטרנזילה: אין עסקה כזאת')
        log = invoice.payment_review_log[-1]
        self.assertEqual((log['action'], log['by'], log['reason']), ('release', user.email, 'נבדק בטרנזילה: אין עסקה כזאת'))
        self.assertTrue(log['at'])

    def test_a_released_charge_is_not_found_again(self):
        # The manager checked: the report's charge was the other website's.
        invoice = self.invoice()
        _opened(invoice, 15)
        self.day_rows = [paid_row(index='555555')]
        self.assertEqual(self.initiate().status_code, 409)
        manager, _user = _manager_client()
        manager.post(self.url(invoice), {'action': 'release', 'reason': 'שייך לאתר השני'}, format='json')
        self.assertIn('iframe_url', self.initiate().json())
        sweep = payment_followup.sweep_stuck_store_payments()
        self.assertEqual(sweep['unexplained'], [])

    def test_complete_after_verification_needs_the_report(self):
        invoice = self.in_review()
        manager, _user = _manager_client()
        res = manager.post(self.url(invoice), {'action': 'complete', 'reason': 'הלקוח שלח אסמכתא'}, format='json')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.state(invoice), ('pending', 0, 10))

        self.ledger_rows = [paid_row(index='999999', approval='0009999')]
        res = manager.post(self.url(invoice), {'action': 'complete', 'reason': 'הלקוח שלח אסמכתא'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_review_log[-1]['action'], 'complete')

    def test_a_suspected_charge_can_be_completed_by_a_person_through_the_report(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        self.day_rows = [paid_row(index='555555')]
        self.initiate()  # 409, kept as suspected
        self.ledger_rows = [paid_row(index='555555')]
        manager, _user = _manager_client()
        # Round 4: only with the customer's own evidence — here the approval number.
        res = manager.post(self.url(invoice), {'action': 'complete', 'reason': 'נבדק מול טרנזילה — זו העסקה',
                                               'confirmation_code': '0001234'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

    def test_a_reason_is_required_and_only_a_manager_may(self):
        invoice = self.in_review()
        manager, _user = _manager_client()
        self.assertEqual(manager.post(self.url(invoice), {'action': 'release'}, format='json').status_code, 400)
        worker, _user = _manager_client(role=UserProfile.ROLE_WORKER, email='worker@example.com')
        self.assertIn(worker.post(self.url(invoice), {'action': 'release', 'reason': 'x'}, format='json').status_code, (403, 404))
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'pending')

    def test_the_invoice_list_says_which_are_in_review(self):
        invoice = self.in_review()
        manager, _user = _manager_client()
        row = manager.get(f'/api/v1/store/invoices/{invoice.pk}/').json()
        self.assertTrue(row['payment_in_review'])
        self.assertEqual([n['index'] for n in row['payment_review_numbers']], ['999999'])


# ---------------------------------------------------------------------------
# P6, P7 — the morning sweep with its switch off writes nothing
# ---------------------------------------------------------------------------

class SweepSwitchOffTest(ReviewBase):
    def till(self, status, txn):
        return StoreInvoice.objects.create(
            customer_name='לקוח', total_amount=8, payment_method='credit_card', payment_status=status,
            notes=json.dumps([{'product_id': str(self.product.id), 'quantity': 2, 'size': ''}]),
            tranzila_transaction_id=txn, tranzila_confirmation_code=f'000{txn[:4]}', tranzila_terminal='iframe_terminal',
        )

    def columns(self, invoice):
        return StoreInvoice.objects.filter(pk=invoice.pk).values(
            'payment_status', 'payment_followup_at', 'other_transactions', 'tranzila_transaction_id').get()

    def test_P6_a_till_invoice_keeps_its_status(self):
        till = self.till('failed', '313131')
        before = self.columns(till)
        payment_followup.sweep_stuck_store_payments()
        self.assertEqual(self.columns(till), before)

    def test_P7_check_now_reads_and_tells_and_writes_nothing(self):
        from apps.core.daily_brief import run_check

        web = self.invoice(txn='123456', code='0001234', age=timedelta(minutes=20))
        till = self.till('pending', '424242')
        StoreInvoice.objects.filter(pk=till.pk).update(payment_reported_at=timezone.now() - timedelta(minutes=20))
        self.ledger_rows = [paid_row(), paid_row(index='424242', approval='0004242')]
        before = (self.columns(web), self.columns(till))
        with patch('apps.core.payment_service._sign_store_sale') as sign, \
                patch('apps.store.tranzila_store_invoice.issue_store_tranzila_document') as doc, \
                patch('apps.store.invoice_email.send_store_invoice_email') as mail, \
                self.captureOnCommitCallbacks(execute=True):
            item = run_check('stuck_store_payments')
        self.assertEqual(StoreSale.objects.count(), 0)
        self.assertEqual((sign.call_count, doc.call_count, mail.call_count), (0, 0, 0))
        self.assertEqual((self.columns(web), self.columns(till)), before)
        self.assertEqual(item['severity'], 'red')
        self.assertEqual(sorted(a.kind for a in OfficeAlert.objects.all()), ['store_payment_confirmed'] * 2)


# ---------------------------------------------------------------------------
# Items 8, 9 — further numbers are known everywhere; refund_failed is paid
# ---------------------------------------------------------------------------

class ElsewhereTest(ReviewBase):
    def test_refund_failed_is_still_paid(self):
        invoice = self.invoice(status='refund_failed', txn='123456', code='0001234')
        self.assertEqual(self.poll().json(), {
            'status': 'completed', 'invoice_number': invoice.invoice_number, 'paid': True, 'payment_reported': False,
        })

    def test_a_second_charge_is_another_orders_payment_for_payment_links(self):
        from apps.payment_links.public_views import _index_paid_for_something_else

        other = self.invoice(status='completed', txn='111111', code='0001111')
        StoreInvoice.objects.filter(pk=other.pk).update(other_transactions=[{
            'index': '222222', 'code': '0002222', 'terminal': 'iframe_terminal',
            'reported_at': timezone.now().isoformat(), 'state': 'second_charge',
        }])
        self.assertTrue(_index_paid_for_something_else('00000000-0000-0000-0000-000000000000', '222222', 'iframe_terminal'))

    def test_the_transaction_check_tool_knows_further_numbers(self):
        from apps.core.tranzila_check import check_transaction

        invoice = self.invoice(status='completed', txn='111111', code='0001111')
        StoreInvoice.objects.filter(pk=invoice.pk).update(other_transactions=[{
            'index': '222222', 'code': '0002222', 'terminal': 'iframe_terminal',
            'reported_at': timezone.now().isoformat(), 'state': 'second_charge',
        }])
        self.ledger_rows = [paid_row(index='222222', approval='0002222')]
        by_index = check_transaction(terminal='iframe_terminal', index='222222')
        self.assertEqual(by_index['ours']['reference'], invoice.invoice_number)
        self.assertEqual(by_index['ours']['number_state'], 'second_charge')
        by_invoice = check_transaction(invoice_number=invoice.invoice_number)
        self.assertEqual([n['index'] for n in by_invoice['ours']['other_transactions']], ['222222'])
