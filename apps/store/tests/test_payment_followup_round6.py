"""
Stage 3, sixth round — what the fifth review found (1.10.2026), part 1: the
reviewer's probes (reviewtests5/test_probes5*.py) P1, Q2, P2, P3, each turned
into a test of the safe behaviour.

What they hold:
  * being asked is not being answered: a released number the report has not
    really answered about opens no second page;
  * "סגור — לא שלנו" never closes a number the report has not answered about,
    and shows the person a charge of this sum before closing over it;
  * a notify the report does not confirm still asks about the order's other
    numbers.
"""
from datetime import timedelta

from django.utils import timezone

from apps.store.models import StoreInvoice
from apps.store.tests.test_payment_followup import paid_row
from apps.store.tests.test_payment_followup_round4 import _opened, row_at
from apps.store.tests.test_payment_followup_round5 import Base, held, second_rows, unpace


def others(invoice):
    invoice.refresh_from_db()
    return {e['index']: e['state'] for e in invoice.other_transactions or []}


def age_numbers(invoice, minutes):
    """The invoice's further numbers were reported this long ago."""
    entries = StoreInvoice.objects.get(pk=invoice.pk).other_transactions or []
    when = (timezone.now() - timedelta(minutes=minutes)).isoformat()
    StoreInvoice.objects.filter(pk=invoice.pk).update(
        other_transactions=[{**entry, 'reported_at': when} for entry in entries])


class ReleasedMustBeAnsweredTest(Base):
    def _released_real_payment(self, page_minutes=20):
        invoice = self.invoice()
        _opened(invoice, 5)
        self.ledger_down = True
        self.notify(invoice, index='999999', ConfirmationCode='0009999')
        self.release(invoice)
        StoreInvoice.objects.filter(pk=invoice.pk).update(
            payment_page_opened_at=timezone.now() - timedelta(minutes=page_minutes),
            payment_page_first_opened_at=timezone.now() - timedelta(minutes=page_minutes))
        age_numbers(invoice, 30)
        unpace(invoice)
        return invoice

    def test_P1a_the_lookup_by_number_fails_while_the_day_list_answers(self):
        invoice = self._released_real_payment()
        self.ledger_down = True
        self.day_rows = [row_at('999999', '0009999', 18)]
        res = self.initiate()
        self.assertEqual(res.status_code, 409)
        self.assertNotIn('iframe_url', res.json(), 'the report did not answer about the released number')
        self.assertTrue(res.json()['retry_after'])
        self.assertIsNone(StoreInvoice.objects.get(pk=invoice.pk).other_transactions[0].get('answered_at'))

    def test_P1b_the_search_is_final_and_the_lookup_fails(self):
        invoice = self._released_real_payment(page_minutes=60 * 50)
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_search_done_at=timezone.now() - timedelta(hours=1))
        self.ledger_down = True
        self.day_report_down = True
        res = self.initiate()
        self.assertEqual(res.status_code, 409)
        self.assertNotIn('iframe_url', res.json())

    def test_P1c_the_report_answers_that_it_does_not_list_it_long_after(self):
        invoice = self._released_real_payment()
        self.ledger_down = False
        self.ledger_rows = []
        self.day_rows = []
        self.assertIn('iframe_url', self.initiate().json())
        self.assertTrue(StoreInvoice.objects.get(pk=invoice.pk).other_transactions[0].get('answered_at'))

    def test_Q2_released_minutes_after_it_was_reported_and_the_report_still_lags(self):
        invoice = self.invoice()
        _opened(invoice, 14, first_minutes=14)
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='999999', ConfirmationCode='0009999')
        self.release(invoice)
        age_numbers(invoice, 3)
        unpace(invoice)
        res = self.initiate()
        self.assertEqual(res.status_code, 409, 'reported three minutes ago: the report may simply not list it yet')
        self.assertNotIn('iframe_url', res.json())
        # Ten minutes after it was reported the report's "not listed" is an answer.
        age_numbers(invoice, 11)
        self.assertIn('iframe_url', self.initiate().json())


class CloseTest(Base):
    def _paid(self):
        invoice = self.invoice()
        _opened(invoice, 3)
        self.ledger_rows = [paid_row()]
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice)
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        return invoice

    def test_P2a_a_number_reported_minutes_ago_is_not_closed_while_the_report_lags(self):
        invoice = self._paid()
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='222222', ConfirmationCode='0002222')  # a second tab; not listed yet
        res = self.review(invoice, 'close', 'לא מופיע בטרנזילה')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(others(invoice), {'222222': 'open'})
        self.assertIn('10 דקות', res.json()['error'])
        # The report catches up: a real second charge, recorded.
        self.ledger_rows.append(paid_row(index='222222', approval='0002222'))
        unpace(invoice)
        self.sweep()
        self.assertEqual(second_rows(invoice).count(), 1)
        self.assertEqual(others(invoice), {'222222': 'second_charge'})

    def test_P2b_a_charge_of_this_sum_in_the_report_is_shown_before_closing(self):
        invoice = self._paid()
        self.ledger_rows.append(paid_row(index='222222', approval='0002222'))
        self.assertEqual(self.returned('222222').status_code, 200)  # no approval number came with it
        manager = self.manager()
        res = self.review(invoice, 'close', 'לא שלנו', manager=manager)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()['charge_shown'], ['222222'])
        self.assertIn('חיוב מאושר', res.json()['error'])
        self.assertEqual(others(invoice), {'222222': 'open'})
        # Having seen it — and only once the number is ten minutes old — the manager may close it.
        res = self.review(invoice, 'close', 'של לקוח אחר', manager=manager, acknowledge_charge=['222222'])
        self.assertEqual(res.status_code, 409, 'reported less than ten minutes ago')
        age_numbers(invoice, 20)
        res = self.review(invoice, 'close', 'של לקוח אחר', manager=manager, acknowledge_charge=['222222'])
        self.assertEqual((res.status_code, res.json()['outcome']), (200, 'closed'))
        entry = StoreInvoice.objects.get(pk=invoice.pk).other_transactions[0]
        self.assertEqual((entry['state'], entry['closed_over_charge']), ('closed', True))

    def test_a_number_the_report_does_not_list_long_after_is_closed_at_once(self):
        invoice = self._paid()
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='424242', ConfirmationCode='0000001')
        age_numbers(invoice, 20)
        res = self.review(invoice, 'close', 'מספר מומצא')
        self.assertEqual((res.status_code, res.json()['outcome']), (200, 'closed'))


class AskOthersAfterNotifyTest(Base):
    def test_P3_a_notify_the_report_does_not_list_yet_still_asks_about_the_released_one(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        self.ledger_down = True
        self.notify(invoice, index='999999', ConfirmationCode='0009999')
        self.release(invoice)
        self.ledger_down = False
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_page_opened_at=timezone.now() - timedelta(minutes=20))
        age_numbers(invoice, 30)
        unpace(invoice)
        self.assertIn('iframe_url', self.initiate().json())
        # The released one was real after all, and the report lists it now;
        # the new page's number is not listed yet.
        self.ledger_rows = [paid_row(index='999999', approval='0009999')]
        unpace(invoice)
        before = len(self.report_calls)
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='222222', ConfirmationCode='0002222')
        self.assertIn('999999', self.report_calls[before:])
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        state = held(invoice)
        self.assertEqual(state['own'], '999999')
        self.assertEqual(state['others'], [('222222', 'open', '0002222')], 'the new number is kept beside it')
