"""
Stage 3, fourth round — what the third review found in the tools round 3 added
(release, suspected charges, widget/payment/returned/): the reviewer's 15 probes
(reviewtests3/test_probes.py), each turned into a test of the safe behaviour.

The two rules they hold:
  * a number not tied to the order with certainty never completes it;
  * a number not decided with certainty never leaves the follow-up.
"""
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from apps.core.models import OfficeAlert, UserProfile
from apps.customers.models import TranzilaTransaction
from apps.store import payment_followup
from apps.store.models import StoreInvoice, StoreSale
from apps.store.tests.test_payment_followup import KEY, paid_row, report_clock
from apps.store.tests.test_payment_followup_review import ORDER, ReviewBase
from apps.store.tests.test_payment_followup_round3 import RETURNED_URL, _manager_client

ORDER_B = 'CG-260929-BBB2'


def _opened(invoice, minutes, order=ORDER, first_minutes=None):
    fields = dict(
        website_idempotency_key=f'idemp-{order}',
        payment_page_opened_at=timezone.now() - timedelta(minutes=minutes),
    )
    if first_minutes is not None:
        fields['payment_page_first_opened_at'] = timezone.now() - timedelta(minutes=first_minutes)
    StoreInvoice.objects.filter(pk=invoice.pk).update(**fields)
    invoice.refresh_from_db()


def row_at(index, approval, minutes_ago, amount='800', **extra):
    return {**paid_row(index=index, approval=approval, amount=amount),
            **report_clock(timezone.now() - timedelta(minutes=minutes_ago)), **extra}


def review_url(invoice):
    return f'/api/v1/store/invoices/{invoice.pk}/payment-review/'


# ---------------------------------------------------------------------------
# R — a person's "release" does not bury a payment
# ---------------------------------------------------------------------------

class ReleaseDoesNotBuryTest(ReviewBase):
    def _in_review_then_released(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        self.ledger_down = True  # the report cannot answer when X is reported
        self.notify(invoice, index='999999', ConfirmationCode='0009999')
        manager, _user = _manager_client()
        with self.captureOnCommitCallbacks(execute=True):
            res = manager.post(review_url(invoice), {'action': 'release', 'reason': 'לא ראיתי עסקה בטרנזילה'},
                               format='json')
        self.assertEqual(res.status_code, 200, res.content)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'failed')
        self.assertEqual(payment_followup.other_state(invoice, '999999'), 'released')
        return invoice

    def test_R1_a_released_charge_the_report_later_confirms_is_a_second_charge(self):
        invoice = self._in_review_then_released()
        self.ledger_down = False
        self.day_rows = []
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_page_opened_at=timezone.now() - timedelta(minutes=20))
        self.assertIn('iframe_url', self.initiate().json(), 'the customer may pay again')
        self.ledger_rows = [paid_row(index='222222', approval='0002222')]
        self.notify(invoice, index='222222', ConfirmationCode='0002222')
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

        # X was real after all: Tranzila repeats its notify, and the report now confirms it.
        self.ledger_rows.append(paid_row(index='999999', approval='0009999'))
        before = len(self.report_calls)
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='999999', ConfirmationCode='0009999')
        self.assertIn('999999', self.report_calls[before:], 'the report is asked')
        invoice.refresh_from_db()
        self.assertEqual(payment_followup.other_state(invoice, '999999'), 'second_charge')
        self.assertTrue(TranzilaTransaction.objects.filter(idempotency_key=f'store_second_{invoice.id}_999999').exists())
        self.assertTrue(OfficeAlert.objects.filter(kind='store_second_charge_confirmed').exists())
        self.assertEqual(self.state(invoice), ('completed', 1, 8), 'sold once')

    def test_R1b_the_sweep_asks_about_released_numbers_too(self):
        invoice = self._in_review_then_released()
        self.ledger_down = False
        self.ledger_rows = [paid_row(index='999999', approval='0009999')]
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_followup_at=None)  # the sweep runs in the morning
        payment_followup.sweep_stuck_store_payments(complete=True)
        self.assertEqual(self.state(invoice), ('completed', 1, 8), 'the report confirms it: the order was paid')

    def test_R2_a_released_number_reported_again_while_the_report_is_down_is_back_in_review(self):
        invoice = self._in_review_then_released()
        self.notify(invoice, index='999999', ConfirmationCode='0009999')  # Tranzila repeats; report still down
        invoice.refresh_from_db()
        self.assertTrue(payment_followup.holds_reported_payment(invoice))
        self.assertEqual(self.initiate().status_code, 409, 'no second page')
        self.ledger_down = False
        self.ledger_rows = [paid_row(index='999999', approval='0009999')]
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_followup_at=None)
        self.assertTrue(self.poll().json()['paid'])
        self.assertEqual(self.state(invoice), ('completed', 1, 8))


# ---------------------------------------------------------------------------
# S — a suspected charge never verifies itself, and never hides from another order
# ---------------------------------------------------------------------------

class SuspectedChargeTest(ReviewBase):
    def _suspected_on(self, invoice, order=ORDER):
        _opened(invoice, 5, order=order)
        z = row_at('555555', '0005555', 1, credit_card_token='ZZZZtoken4242')
        self.day_rows = [z]
        self.ledger_rows = [z]
        self.assertEqual(self.initiate(order=order).status_code, 409)
        invoice.refresh_from_db()
        return invoice

    def test_S1_complete_on_a_suspected_charge_needs_the_customers_evidence(self):
        a = self._suspected_on(self.invoice())
        entry = next(e for e in a.other_transactions if e['index'] == '555555')
        self.assertEqual(entry['code'], '', 'the report\'s own approval is never kept as the evidence')
        manager, _user = _manager_client()
        res = manager.post(review_url(a), {'action': 'complete', 'reason': 'נראה שזה התשלום'}, format='json')
        self.assertEqual(res.status_code, 409)
        res = manager.post(review_url(a), {'action': 'complete', 'reason': 'נראה שזה התשלום',
                                           'confirmation_code': '0000001'}, format='json')
        self.assertEqual(res.status_code, 409, 'an approval number that is not the charge\'s')
        res = manager.post(review_url(a), {'action': 'complete', 'reason': 'נראה שזה התשלום',
                                           'card_last4': '1111'}, format='json')
        self.assertEqual(res.status_code, 409, 'card digits that are not the charge\'s')
        self.assertEqual(self.state(a), ('pending', 0, 10))

        res = manager.post(review_url(a), {'action': 'complete', 'reason': 'הלקוח מסר את מספר האישור',
                                           'confirmation_code': '0005555'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.state(a), ('completed', 1, 8))

    def test_S1_the_last_four_card_digits_are_evidence_too(self):
        a = self._suspected_on(self.invoice())
        manager, _user = _manager_client()
        res = manager.post(review_url(a), {'action': 'complete', 'reason': 'הלקוח מסר 4 ספרות',
                                           'card_last4': '4242'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.state(a), ('completed', 1, 8))

    def test_S1b_the_order_that_really_paid_is_completed_by_its_notify(self):
        b = self.invoice(order=ORDER_B)  # B's customer pays Z
        a = self._suspected_on(self.invoice(order=ORDER))
        manager, _user = _manager_client()
        manager.post(review_url(a), {'action': 'complete', 'reason': 'נראה שזה התשלום'}, format='json')
        self.notify(b, index='555555', ConfirmationCode='0005555')  # B's notify for Z arrives
        self.assertEqual(self.state(b)[:2], ('completed', 1))
        self.assertEqual(self.state(a)[:2], ('pending', 0))

    def test_S2_a_charge_suspected_on_one_order_is_found_for_another(self):
        b = self.invoice(order=ORDER_B)
        _opened(b, 5, order=ORDER_B)
        a = self._suspected_on(self.invoice(order=ORDER))
        res_b = self.initiate(order=ORDER_B)
        self.assertEqual(res_b.status_code, 409, 'Z may be B\'s payment too')
        sweep = payment_followup.sweep_stuck_store_payments(complete=True)
        self.assertEqual(set(i.pk for i in sweep['unexplained']), {a.pk, b.pk})

    def test_S2b_after_one_order_releases_it_the_other_still_finds_it(self):
        b = self.invoice(order=ORDER_B)
        _opened(b, 5, order=ORDER_B)
        a = self._suspected_on(self.invoice(order=ORDER))
        manager, _user = _manager_client()
        manager.post(review_url(a), {'action': 'release', 'reason': 'לא שלו — שם אחר'}, format='json')
        found = payment_followup.find_unreported_payment(StoreInvoice.objects.get(pk=b.pk))
        self.assertEqual(found[0], 'found')


# ---------------------------------------------------------------------------
# L — a notify that never came, and a report that lags
# ---------------------------------------------------------------------------

class ReportLagTest(ReviewBase):
    def test_L1_a_retry_minutes_after_the_page_gets_no_second_page_while_the_report_may_lag(self):
        invoice = self.invoice()
        _opened(invoice, 3)
        self.day_rows = []  # C1 was paid a minute ago; the report does not list it yet
        res = self.initiate()
        self.assertEqual(res.status_code, 409)
        self.assertTrue(res.json()['payment_in_review'])
        self.assertTrue(res.json()['retry_after'] > 0)
        self.assertNotIn('iframe_url', res.json())

    def test_L1_a_charge_of_the_first_page_found_after_the_order_completed_is_told(self):
        invoice = self.invoice(status='completed', txn='222222', code='0002222')
        _opened(invoice, 20, first_minutes=40)  # two pages: the first 40 minutes ago
        StoreSale.objects.create(invoice=invoice, product=self.product, quantity=2, unit_price=4,
                                 total_price=8, payment_method='credit_card')
        self.day_rows = [row_at('111111', '0001111', 35), row_at('222222', '0002222', 15)]
        with self.captureOnCommitCallbacks(execute=True):
            sweep = payment_followup.sweep_stuck_store_payments()
        self.assertIn(invoice, sweep['unexplained'])
        alert = OfficeAlert.objects.get(kind='store_possible_double_charge')
        self.assertIn('111111', alert.what)

    def test_the_search_starts_from_the_first_page(self):
        invoice = self.invoice()
        _opened(invoice, 20, first_minutes=90)
        self.day_rows = [row_at('111111', '0001111', 60)]  # after the first page, before the last
        self.assertEqual(payment_followup.find_unreported_payment(invoice)[0], 'found')

    def test_a_report_read_only_in_part_is_unknown(self):
        invoice = self.invoice()
        _opened(invoice, 20)
        self.day_rows = []
        self.day_report_partial = True
        self.assertEqual(payment_followup.find_unreported_payment(invoice)[0], 'unknown')


# ---------------------------------------------------------------------------
# W — widget/payment/returned/ opens no hole
# ---------------------------------------------------------------------------

class ReturnedGuardsTest(ReviewBase):
    def test_W1_a_flood_of_numbers_is_kept_told_once_and_the_sweep_stays_bounded(self):
        invoice = self.invoice(status='completed', txn='123456', code='0001234')
        _opened(invoice, 5)
        StoreSale.objects.create(invoice=invoice, product=self.product, quantity=2, unit_price=4,
                                 total_price=8, payment_method='credit_card')
        with self.captureOnCommitCallbacks(execute=True):
            for i in range(20):
                self.client.post(RETURNED_URL, {'order': ORDER, 'index': str(700000 + i)}, format='json', **KEY)
        invoice.refresh_from_db()
        # Round 5: the limit is on the work, not on what is kept — a real
        # number arriving after three made-up ones must not be dropped.
        self.assertEqual(len(invoice.other_transactions), 20)
        self.assertLessEqual(len(invoice.other_transactions), payment_followup.MAX_KEPT_NUMBERS)
        self.assertEqual(OfficeAlert.objects.filter(kind='store_possible_double_charge').count(), 1)
        self.assertEqual(OfficeAlert.objects.filter(kind='store_too_many_numbers').count(), 1)
        before = len(self.report_calls)
        payment_followup.sweep_stuck_store_payments()
        self.assertLessEqual(len(self.report_calls) - before, payment_followup.MAX_REPORT_READS_PER_INVOICE)

    def test_W2_a_number_for_an_order_with_no_recent_page_is_refused(self):
        victim = self.invoice()
        _opened(victim, 3 * 60)
        res = self.client.post(RETURNED_URL, {'order': ORDER, 'index': '1'}, format='json', **KEY)
        self.assertEqual(res.status_code, 409)
        victim.refresh_from_db()
        self.assertEqual(victim.tranzila_transaction_id, '')
        self.assertFalse(payment_followup.holds_reported_payment(victim))

    def test_W3_only_ascii_digits_are_a_number(self):
        victim = self.invoice()
        _opened(victim, 5)
        res = self.client.post(RETURNED_URL, {'order': ORDER, 'index': '١٢٣'}, format='json', **KEY)
        self.assertEqual(res.status_code, 400)
        self.notify(victim, index='١٢٣', ConfirmationCode='0001234')
        victim.refresh_from_db()
        self.assertEqual(victim.tranzila_transaction_id, '')
        self.assertFalse(payment_followup.holds_reported_payment(victim))


# ---------------------------------------------------------------------------
# P, D, O — the reviewer's checks that already held (kept as guards)
# ---------------------------------------------------------------------------

class GuardsTest(ReviewBase):
    def test_P_partner_and_worker_are_refused(self):
        invoice = self.invoice()
        self.ledger_down = True
        self.notify(invoice, index='999999', ConfirmationCode='0009999')
        partner, _u = _manager_client(role=UserProfile.ROLE_PARTNER, email='partner@example.com')
        worker, _w = _manager_client(role=UserProfile.ROLE_WORKER, email='worker2@example.com')
        self.assertEqual(partner.post(review_url(invoice), {'action': 'release', 'reason': 'בדיקה'},
                                      format='json').status_code, 403)
        self.assertIn(worker.post(review_url(invoice), {'action': 'complete', 'reason': 'בדיקה'},
                                  format='json').status_code, (403, 404))

    def test_D_a_decline_on_an_order_holding_only_a_suspected_charge_stays_in_review(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        self.day_rows = [row_at('555555', '0005555', 1)]
        self.initiate()
        self.notify(invoice, Response='033', index='', ConfirmationCode='')
        invoice.refresh_from_db()
        self.assertTrue(payment_followup.holds_reported_payment(invoice))

    @override_settings(STORE_SWEEP_COMPLETES_PAYMENTS=False)
    def test_O_switch_off_sweep_sells_nothing_and_changes_no_status(self):
        web = self.invoice(txn='123456', code='0001234', age=timedelta(minutes=20))
        lost = self.invoice(order=ORDER_B)
        _opened(lost, 45, order=ORDER_B)
        self.day_rows = [row_at('555555', '0005555', 40)]
        self.ledger_rows = [paid_row()]
        cols = ('payment_status', 'payment_followup_at', 'other_transactions', 'tranzila_transaction_id',
                'payment_reported_at', 'payment_review_log')
        before = StoreInvoice.objects.filter(pk=web.pk).values(*cols).get()
        payment_followup.sweep_stuck_store_payments()
        # An invoice in review, which the report confirms: not written to at all.
        self.assertEqual(before, StoreInvoice.objects.filter(pk=web.pk).values(*cols).get())
        # A charge found for a lost page is kept on its invoice (round 5) —
        # and nothing else about it changes.
        lost.refresh_from_db()
        self.assertEqual((lost.payment_status, lost.tranzila_transaction_id, lost.payment_followup_at,
                          lost.payment_reported_at, lost.payment_review_log), ('pending', '', None, None, None))
        self.assertEqual([(e['index'], e['state'], e['code']) for e in lost.other_transactions],
                         [('555555', 'suspected', '')])
        self.assertEqual(StoreSale.objects.count(), 0)


# ---------------------------------------------------------------------------
# C — widget/payment/returned/ versus the notify
# ---------------------------------------------------------------------------

class ReturnedVersusNotifyTest(ReviewBase):
    def test_C1_a_wrong_code_before_the_real_notify_raises_no_alarm(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        self.ledger_rows = [paid_row()]
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(RETURNED_URL, {'order': ORDER, 'index': '123456', 'code': '9999999'},
                             format='json', **KEY)
            self.notify(invoice)
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        self.assertNotIn('store_payment_unverified', self.kinds())

    def test_C2_a_manager_completes_with_the_customers_card_digits(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        self.ledger_rows = [{**paid_row(), 'credit_card_token': 'AAAAtoken4321'}]
        self.client.post(RETURNED_URL, {'order': ORDER, 'index': '123456'}, format='json', **KEY)
        manager, _user = _manager_client()
        res = manager.post(review_url(invoice), {'action': 'complete', 'reason': 'הלקוח שילם'}, format='json')
        self.assertEqual(res.status_code, 409)
        self.assertIn('4 ספרות', res.json()['error'])
        res = manager.post(review_url(invoice), {'action': 'complete', 'reason': 'הלקוח שילם', 'card_last4': '4321'},
                           format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
