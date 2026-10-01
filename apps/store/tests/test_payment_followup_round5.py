"""
Stage 3, fifth round — what the fourth review found (1.10.2026): the
reviewer's probes (reviewtests4/test_probes4*.py), each turned into a test of
the safe behaviour, that failed before this round.

What they hold:
  * the limit on numbers is a limit on work, never on what is kept: every
    reported number is kept, and the numbers of one order take turns at the
    report, so each is asked in the end;
  * a number a person released is asked about before a second page leaves, by
    the site's poll and by another number's notify — and a person can still
    complete it;
  * a report that answers an error, or cannot be read in full, is "unknown",
    never "no charge": no second page on it, however long it lasts;
  * a charge found in the report is kept on the invoice until a person
    decides — on a paid order too, and whatever the sweep's switch reads;
  * a second charge the report confirms is recorded and told as confirmed,
    whatever the sweep's switch reads.
"""
from datetime import date, timedelta
from unittest.mock import patch

from django.utils import timezone

from apps.core.daily_brief import check_stuck_store_payments
from apps.core.models import OfficeAlert
from apps.core.tranzila_service import TranzilaService
from apps.customers.models import TranzilaTransaction
from apps.store import payment_followup
from apps.store.models import StoreInvoice, StoreSale
from apps.store.tests.test_payment_followup import INITIATE_URL, KEY, FollowupBase, paid_row
from apps.store.tests.test_payment_followup_review import ORDER, ReviewBase, payload, reported_minutes_ago
from apps.store.tests.test_payment_followup_round3 import RETURNED_URL, _manager_client
from apps.store.tests.test_payment_followup_round4 import _opened, review_url, row_at

READS = payment_followup.MAX_REPORT_READS_PER_INVOICE


def held(invoice):
    invoice.refresh_from_db()
    return {
        'status': invoice.payment_status,
        'own': invoice.tranzila_transaction_id,
        'code': invoice.tranzila_confirmation_code,
        'others': [(e['index'], e['state'], e.get('code')) for e in invoice.other_transactions or []],
    }


def unpace(invoice):
    StoreInvoice.objects.filter(pk=invoice.pk).update(payment_followup_at=None)


def second_rows(invoice=None):
    rows = TranzilaTransaction.objects.filter(idempotency_key__startswith='store_second_')
    return rows.filter(idempotency_key__startswith=f'store_second_{invoice.id}_') if invoice else rows


class Base(ReviewBase):
    def setUp(self):
        super().setUp()
        self._managers = 0

    def manager(self):
        self._managers += 1
        return _manager_client(email=f'manager{self._managers}@example.com')[0]

    def review(self, invoice, action, reason='נבדק בטרנזילה', manager=None, **body):
        with self.captureOnCommitCallbacks(execute=True):
            return (manager or self.manager()).post(
                review_url(invoice), {'action': action, 'reason': reason, **body}, format='json')

    def release(self, invoice, reason='נבדק בטרנזילה, אין חיוב'):
        res = self.review(invoice, 'release', reason)
        self.assertEqual(res.status_code, 200, res.content)

    def returned(self, index, code=None, order=ORDER):
        body = {'order': order, 'index': index}
        if code is not None:
            body['code'] = code
        return self.client.post(RETURNED_URL, body, format='json', **KEY)

    def sweep(self, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return payment_followup.sweep_stuck_store_payments(**kwargs)

    def assert_alerts_fit(self):
        """Every alert raised fits the office template's fields (a longer one would not be sent)."""
        limits = {'title': 200, 'where': 300, 'what': 500, 'why': 500, 'customer': 500, 'action': 300, 'link': 300}
        for alert in OfficeAlert.objects.all():
            for field, limit in limits.items():
                self.assertLessEqual(len(getattr(alert, field) or ''), limit, f'{alert.kind}.{field}')


# ---------------------------------------------------------------------------
# 1 — the limit is on work, not on what is kept
# ---------------------------------------------------------------------------

class EveryNumberIsKeptTest(Base):
    def test_A1_three_made_up_numbers_do_not_push_out_the_real_one(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        self.ledger_rows = []  # the report lists none of them
        with self.captureOnCommitCallbacks(execute=True):
            for fake in ('111', '222', '333'):
                self.notify(invoice, index=fake, ConfirmationCode='0000001')
            # The customer's real payment; the report does not list it yet.
            self.notify(invoice, index='777777', ConfirmationCode='0007777')
        state = held(invoice)
        self.assertIn('777777', {state['own']} | {i for i, _s, _c in state['others']}, 'the real number is kept')
        self.assertEqual(self.kinds().count('store_too_many_numbers'), 1, 'told once, when the fourth came')

        self.ledger_rows = [paid_row(index='777777', approval='0007777')]  # the report catches up
        unpace(invoice)
        self.assertTrue(self.poll().json()['paid'])
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        self.assert_alerts_fit()

    def test_A2_released_suspected_charges_do_not_push_out_the_real_notify(self):
        # No attacker: three charges of the same sum by other customers of the shared terminal.
        invoice = self.invoice()
        _opened(invoice, 15)
        self.day_rows = [row_at('501', '0000501', 3), row_at('502', '0000502', 4), row_at('503', '0000503', 5)]
        self.ledger_rows = list(self.day_rows)
        self.assertEqual(self.initiate().status_code, 409)
        self.release(invoice, 'שלושתם של האתר השני')
        self.assertIn('iframe_url', self.initiate().json(), 'the customer may pay now')
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='777777', ConfirmationCode='0007777')  # real; the report lags
        state = held(invoice)
        self.assertEqual(state['own'], '777777')
        self.assertNotIn('store_too_many_numbers', self.kinds(), 'found or released numbers are not a flood')

        self.ledger_rows.append(paid_row(index='777777', approval='0007777'))
        unpace(invoice)
        self.assertTrue(self.poll().json()['paid'])
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

    def test_A3_a_number_the_report_confirms_at_once_completes_whatever_is_kept(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        for fake in ('111', '222', '333'):
            self.notify(invoice, index=fake, ConfirmationCode='0000001')
        self.ledger_rows = [paid_row(index='777777', approval='0007777')]
        self.notify(invoice, index='777777', ConfirmationCode='0007777')
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        self.assertEqual(held(invoice)['own'], '777777')

    def test_A4_many_suspected_charges_are_all_kept_and_a_decision_reads_the_report_a_few_times(self):
        invoice = self.invoice()
        _opened(invoice, 15)
        self.day_rows = [row_at(str(600 + i), f'{600 + i:07d}', 3) for i in range(12)]
        self.ledger_rows = list(self.day_rows)
        self.assertEqual(self.initiate().status_code, 409)
        self.assertEqual(len(held(invoice)['others']), 12, 'every charge found is kept')

        manager = self.manager()
        before = len(self.report_calls)
        res = self.review(invoice, 'complete', manager=manager, card_last4='4242')
        self.assertEqual(res.status_code, 409)
        self.assertLessEqual(len(self.report_calls) - before, READS, 'a bounded number of report reads')
        self.assertGreater(res.json()['not_asked'], 0)
        self.assertIn('לחצו שוב', res.json()['error'])
        first = set(self.report_calls[before:])
        before = len(self.report_calls)
        self.review(invoice, 'complete', manager=manager, card_last4='4242')
        self.assertFalse(first & set(self.report_calls[before:]), 'the next call asks about the next numbers')

    def test_a_stream_of_made_up_notifies_costs_one_report_read_each(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        for i in range(8):
            self.notify(invoice, index=str(9000 + i), ConfirmationCode='0000001')
        self.assertEqual(len(self.report_calls), 8, "the order's other numbers are asked about at the follow-up's pace")
        unpace(invoice)
        before = len(self.report_calls)
        self.poll()
        self.assertEqual(len(self.report_calls) - before, READS, 'and one check never reads more than its limit')

    def test_U1_returned_numbers_for_a_paid_order_are_all_kept_and_told_once(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        self.ledger_rows = [paid_row()]
        self.notify(invoice)
        with self.captureOnCommitCallbacks(execute=True):
            for fake in ('1', '2', '3', '4', '5'):
                self.assertEqual(self.returned(fake, '0000001').status_code, 200)
        self.assertEqual([i for i, _s, _c in held(invoice)['others']], ['1', '2', '3', '4', '5'])
        self.assertEqual(self.kinds().count('store_possible_double_charge'), 1)
        self.assertEqual(self.kinds().count('store_too_many_numbers'), 1)
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

    def test_beyond_the_high_limit_a_new_unconfirmed_number_is_not_kept_and_a_confirmed_one_is(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        with patch.object(payment_followup, 'MAX_KEPT_NUMBERS', 4), self.captureOnCommitCallbacks(execute=True):
            for fake in ('1', '2', '3', '4', '5', '6'):
                self.notify(invoice, index=fake, ConfirmationCode='0000001')
            state = held(invoice)
            self.assertEqual([state['own']] + [i for i, _s, _c in state['others']], ['1', '2', '3', '4'])
            self.ledger_rows = [paid_row(index='777777', approval='0007777')]
            self.notify(invoice, index='777777', ConfirmationCode='0007777')
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        self.assertEqual(held(invoice)['own'], '777777')
        self.assertEqual(self.kinds().count('store_too_many_numbers'), 1)
        self.assert_alerts_fit()


class NumbersTakeTurnsTest(Base):
    def _six_released(self):
        invoice = self.invoice(status='failed')
        _opened(invoice, 600)
        entries = [{'index': str(700 + i), 'code': f'{700 + i:07d}', 'terminal': 'iframe_terminal',
                    'reported_at': (timezone.now() - timedelta(hours=10)).isoformat(), 'state': 'released'}
                   for i in range(6)]
        StoreInvoice.objects.filter(pk=invoice.pk).update(other_transactions=entries)
        self.ledger_rows = [paid_row(index='705', approval='0000705')]  # the sixth is the real one
        return invoice

    def test_M1_the_sixth_number_is_asked_on_the_second_morning(self):
        invoice = self._six_released()
        asked = []
        for _morning in range(2):
            unpace(invoice)
            before = len(self.report_calls)
            self.sweep(complete=True)
            asked.append(self.report_calls[before:])
        self.assertEqual(asked[0], ['700', '701', '702', '703'], 'a few report reads per order per morning')
        self.assertIn('705', asked[1])
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

    def test_M1_also_with_the_switch_off(self):
        invoice = self._six_released()
        self.sweep()
        self.assertNotIn('store_payment_confirmed', self.kinds())
        self.sweep()
        self.assertIn('705', self.report_calls)
        self.assertIn('store_payment_confirmed', self.kinds(), 'the real one is found and told')
        self.assertEqual(self.state(invoice), ('failed', 0, 10), 'switch off: nothing sold, no status')

    def test_a_retry_with_more_released_numbers_than_one_check_asks_waits_and_then_gets_its_page(self):
        invoice = self._six_released()
        self.ledger_rows = []  # none of them is in the report
        self.day_rows = []
        res = self.initiate()
        self.assertEqual(res.status_code, 409, 'two of the six were not asked about yet')
        self.assertTrue(res.json()['retry_after'])
        self.assertIn('iframe_url', self.initiate().json(), 'the next retry asks about the last two')
        self.assertEqual(sorted(set(self.report_calls)), ['700', '701', '702', '703', '704', '705'])


# ---------------------------------------------------------------------------
# The approval number: the notify's own wins, the site's only fills a blank
# ---------------------------------------------------------------------------

class ApprovalNumberTest(Base):
    def _then_report_catches_up(self, invoice):
        self.ledger_rows = [paid_row()]
        unpace(invoice)
        return self.poll().json()

    def test_B1_return_without_code_first_then_the_notify_under_lag(self):
        invoice = self.invoice()
        _opened(invoice, 2)
        self.assertEqual(self.returned('123456').status_code, 200)
        self.notify(invoice)  # the real notify, code 0001234; the report still lags
        self.assertEqual(held(invoice)['code'], '0001234')
        self.assertTrue(self._then_report_catches_up(invoice)['paid'])

    def test_B2_return_with_a_wrong_code_first_then_the_notify_under_lag(self):
        invoice = self.invoice()
        _opened(invoice, 2)
        self.assertEqual(self.returned('123456', '9999999').status_code, 200)
        self.notify(invoice)
        self.assertEqual(held(invoice)['code'], '0001234', "the notify's own approval number wins")
        self.assertTrue(self._then_report_catches_up(invoice)['paid'])

    def test_B3_notify_first_then_a_return_with_a_wrong_code(self):
        invoice = self.invoice()
        _opened(invoice, 2)
        self.notify(invoice)
        self.assertEqual(self.returned('123456', '9999999').status_code, 200)
        self.assertEqual(held(invoice)['code'], '0001234', "the site's code never writes over the notify's")
        self.assertTrue(self._then_report_catches_up(invoice)['paid'])

    def test_B4_a_further_number_gets_the_notifys_code_too(self):
        invoice = self.invoice()
        _opened(invoice, 2)
        self.notify(invoice, index='111111', ConfirmationCode='0001111')  # some other number first, own
        self.returned('123456', '9999999')  # the real one, beside it, with a wrong code
        self.notify(invoice)  # the real notify with the code, under lag
        self.assertIn(('123456', 'open', '0001234'), held(invoice)['others'])
        self.assertTrue(self._then_report_catches_up(invoice)['paid'])


# ---------------------------------------------------------------------------
# 3 — a report that answers an error is not an empty day
# ---------------------------------------------------------------------------

class ReportErrorTest(FollowupBase):
    ENVELOPE = {'error_code': 20002, 'message': 'Invalid application key'}

    def test_C1_an_error_envelope_is_not_an_empty_day(self):
        today = date.today()
        with patch.object(TranzilaService, '_make_api_request', return_value=self.ENVELOPE):
            raw = TranzilaService.iframe().list_all_transactions(today, today, max_pages=3)
            rows = payment_followup._day_report(today, today)
        self.assertFalse(raw['success'])
        self.assertIs(raw['complete'], False)
        self.assertIsNone(rows, 'a refused key says nothing about the day')

    def test_an_answer_with_no_list_of_rows_is_not_complete(self):
        today = date.today()
        with patch.object(TranzilaService, '_make_api_request', return_value={'error_code': 0, 'message': 'Success'}):
            raw = TranzilaService.iframe().list_all_transactions(today, today, max_pages=3)
            rows = payment_followup._day_report(today, today)
        self.assertIs(raw['complete'], False)
        self.assertIsNone(rows)

    def test_an_empty_list_is_an_empty_day(self):
        today = date.today()
        with patch.object(TranzilaService, '_make_api_request',
                          return_value={'error_code': 0, 'message': 'Success', 'transactions': []}):
            self.assertEqual(payment_followup._day_report(today, today), [])

    def test_C2_no_second_page_on_an_error_envelope(self):
        invoice = self.invoice()
        StoreInvoice.objects.filter(pk=invoice.pk).update(
            website_idempotency_key=f'idemp-{ORDER}', payment_page_opened_at=timezone.now() - timedelta(minutes=11),
            payment_page_first_opened_at=timezone.now() - timedelta(minutes=11))
        with patch.object(TranzilaService, '_make_api_request', return_value=self.ENVELOPE), \
                patch.object(TranzilaService, 'create_handshake_token', return_value='thtk'), \
                self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(INITIATE_URL, payload(), format='json', **KEY)
        self.assertEqual(res.status_code, 409, 'the report could not be asked: no second page')
        self.assertNotIn('iframe_url', res.json())
        self.assertEqual([a.kind for a in OfficeAlert.objects.all()], ['store_report_unavailable'])


# ---------------------------------------------------------------------------
# 6 — a report that stays down, or is read in part, keeps blocking
# ---------------------------------------------------------------------------

class ReportUnavailableTest(Base):
    def test_K1_report_down_for_thirty_one_minutes(self):
        invoice = self.invoice()
        _opened(invoice, 31, first_minutes=31)
        self.day_report_down = True
        with self.captureOnCommitCallbacks(execute=True):
            res = self.initiate()
            self.assertEqual(self.initiate().status_code, 409)
        self.assertEqual(res.status_code, 409)
        self.assertNotIn('iframe_url', res.json())
        self.assertTrue(res.json()['retry_after'])
        self.assertEqual(self.kinds(), ['store_report_unavailable'], 'one alert, however often the customer retries')
        alert = OfficeAlert.objects.get()
        self.assertEqual(alert.title, 'הדוח של טרנזילה לא זמין — לקוח מחכה')
        self.assert_alerts_fit()

        self.day_report_down = False  # the report answers again: the page is handed out
        self.assertIn('iframe_url', self.initiate().json())

    def test_K2_report_partial_for_thirty_one_minutes(self):
        invoice = self.invoice()
        _opened(invoice, 31, first_minutes=31)
        self.day_report_partial = True
        with self.captureOnCommitCallbacks(execute=True):
            res = self.initiate()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.kinds(), ['store_report_unavailable'])

    def test_the_sweep_lists_what_it_could_not_look_for(self):
        invoice = self.invoice()
        _opened(invoice, 60, first_minutes=60)
        self.day_report_partial = True
        sweep = self.sweep()
        self.assertEqual(sweep['not_searched'], [invoice])
        with patch('apps.store.payment_followup.sweep_stuck_store_payments', return_value=sweep):
            item = check_stuck_store_payments(date.today())
        self.assertEqual(item.count, 1)
        self.assertIn('לא נקרא במלואו', str(item.rows[0]))


# ---------------------------------------------------------------------------
# 2 — a released number is asked about, and can still be completed
# ---------------------------------------------------------------------------

class ReleasedNumberTest(Base):
    def _released_real_payment(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        self.ledger_down = True
        self.notify(invoice, index='999999', ConfirmationCode='0009999')
        self.release(invoice)
        self.ledger_down = False
        self.ledger_rows = [paid_row(index='999999', approval='0009999')]  # it was real after all
        self.day_rows = [row_at('999999', '0009999', 4)]
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_page_opened_at=timezone.now() - timedelta(minutes=20))
        unpace(invoice)
        return invoice

    def test_D1_a_retry_asks_about_released_numbers_before_a_second_page(self):
        invoice = self._released_real_payment()
        before = len(self.report_calls)
        res = self.initiate()
        self.assertIn('999999', self.report_calls[before:])
        self.assertNotIn('iframe_url', res.json(), 'the report confirms the released number: no second page')
        self.assertTrue(res.json().get('already_paid'))
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

    def test_D2_switch_off_the_sweep_tells_and_the_sites_poll_completes(self):
        invoice = self._released_real_payment()
        self.sweep()  # switch off (default)
        self.assertIn('store_payment_confirmed', self.kinds())
        self.assertEqual(self.state(invoice), ('failed', 0, 10))
        alert = OfficeAlert.objects.get(kind='store_payment_confirmed')
        self.assertIn('השלם אחרי אימות', alert.action)
        unpace(invoice)
        self.assertTrue(self.poll().json()['paid'], 'the poll asks about released numbers too')
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        self.assert_alerts_fit()

    def test_D2_a_manager_can_complete_a_released_number_the_report_confirms(self):
        invoice = self._released_real_payment()
        self.assertEqual(self.review(invoice, 'complete', 'הדוח מאשר').status_code, 200)
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        self.assertEqual(held(invoice)['own'], '999999')

    def test_D3_another_numbers_notify_asks_about_the_released_one_too(self):
        invoice = self._released_real_payment()
        self.ledger_rows = []  # the report answers, and does not list it when the customer retries
        self.day_rows = []
        reported_minutes_ago(invoice, 20)
        self.assertIn('iframe_url', self.initiate().json())
        self.ledger_rows = [paid_row(index='999999', approval='0009999'), paid_row(index='222222', approval='0002222')]
        unpace(invoice)  # the customer takes more than the follow-up's fifteen seconds to pay
        before = len(self.report_calls)
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='222222', ConfirmationCode='0002222')
        self.assertIn('999999', self.report_calls[before:])
        self.assertLessEqual(len(self.report_calls) - before, READS)
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        self.assertEqual(held(invoice)['others'], [('999999', 'second_charge', '0009999')])
        self.assertEqual(second_rows(invoice).count(), 1)
        self.assertIn('store_second_charge_confirmed', self.kinds())

    def test_U2_a_decline_after_a_release_and_a_new_page_leaves_the_released_number_released(self):
        invoice = self.invoice()
        _opened(invoice, 30)
        self.ledger_down = True
        self.notify(invoice, index='999999', ConfirmationCode='0009999')
        self.release(invoice)
        self.ledger_down = False
        reported_minutes_ago(invoice, 20)
        self.assertIn('iframe_url', self.initiate().json())
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, Response='033', index='', ConfirmationCode='')
        self.assertEqual(held(invoice)['others'], [('999999', 'released', '0009999')])
        self.assertEqual(held(invoice)['status'], 'failed')


# ---------------------------------------------------------------------------
# 5 — switch off: a second charge the report confirms is recorded
# ---------------------------------------------------------------------------

class SecondChargeSwitchOffTest(Base):
    def test_E1_a_confirmed_second_charge_is_recorded_and_told_as_confirmed(self):
        invoice = self.invoice()
        self.ledger_rows = [paid_row()]
        self.notify(invoice)
        self.assertEqual(self.state(invoice)[0], 'completed')
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='222222', ConfirmationCode='0002222')  # the report lags
        self.ledger_rows.append(paid_row(index='222222', approval='0002222'))
        unpace(invoice)
        with patch('apps.store.invoice_email.send_store_invoice_email') as mail, \
                patch('apps.store.tranzila_store_invoice.issue_store_tranzila_document') as document:
            sweep = self.sweep()  # switch off
        self.assertEqual(sweep['second_confirmed'], [invoice])
        self.assertEqual(sweep['second_open'], [])
        self.assertEqual(held(invoice)['others'], [('222222', 'second_charge', '0002222')])
        self.assertTrue(second_rows(invoice).filter(idempotency_key=f'store_second_{invoice.id}_222222').exists())
        alert = OfficeAlert.objects.get(kind='store_second_charge_confirmed')
        self.assertEqual(alert.title, 'חיוב שני מאושר בחנות — לזכות')
        self.assertIn('222222', alert.what)
        self.assertEqual(self.state(invoice), ('completed', 1, 8), 'nothing sold again')
        mail.assert_not_called()
        document.assert_not_called()
        self.assert_alerts_fit()

        with patch('apps.store.payment_followup.sweep_stuck_store_payments', return_value=sweep):
            item = check_stuck_store_payments(date.today())
        text = ' '.join(str(row) for row in item.rows)
        self.assertIn('חיוב שני מאושר', text)
        self.assertNotIn('עוד לא הכריע', text)

    def test_T1_two_pages_both_paid_one_sale_one_second_charge(self):
        invoice = self.invoice()
        _opened(invoice, 20, first_minutes=40)
        self.ledger_rows = [paid_row(index='111111', approval='0001111'), paid_row(index='222222', approval='0002222')]
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='111111', ConfirmationCode='0001111')
            self.notify(invoice, index='222222', ConfirmationCode='0002222')
            self.notify(invoice, index='222222', ConfirmationCode='0002222')
            self.notify(invoice, index='111111', ConfirmationCode='0001111')
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        self.assertEqual(second_rows().count(), 1)
        self.assertEqual(self.kinds().count('store_second_charge_confirmed'), 1)
        self.assertEqual(len([c for c in self.site.paid_calls if c['body'].get('status') == 'paid']), 1)


class SwitchOffSellsNothingTest(Base):
    def _snapshot(self, invoice):
        invoice.refresh_from_db()
        return (invoice.payment_status, invoice.tranzila_transaction_id, invoice.tranzila_confirmation_code,
                invoice.payment_followup_at, invoice.payment_reported_at, invoice.other_transactions,
                invoice.payment_review_log, invoice.website_paid_notified_at)

    def test_O1_a_till_invoice_is_only_told_and_a_manager_can_complete_it(self):
        invoice = self.invoice(order=None)
        # The till's secure page was handed out for it (round 6: only then may a notify pay it).
        StoreInvoice.objects.filter(pk=invoice.pk).update(
            website_order_number=None, payment_page_opened_at=timezone.now() - timedelta(minutes=31))
        self.ledger_down = True
        self.notify(invoice)
        self.ledger_down = False
        self.ledger_rows = [paid_row()]
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_reported_at=timezone.now() - timedelta(minutes=30))
        unpace(invoice)
        before = self._snapshot(invoice)
        sweep = self.sweep()
        self.assertEqual(before, self._snapshot(invoice), 'switch off: an invoice in review is not written to')
        self.assertEqual(sweep['confirmed'], [invoice])
        alert = OfficeAlert.objects.get(kind='store_payment_confirmed')
        self.assertIn('בקופה אין מי שישאל', alert.action)
        self.assertEqual(self.review(invoice, 'complete', 'הדוח מאשר').status_code, 200)
        self.assertEqual(StoreSale.objects.filter(invoice=invoice).count(), 1)

    def test_O2_the_sweep_sells_nothing_and_keeps_what_it_learned(self):
        # paid + open second (confirmed), failed + released (confirmed), a lost page with a charge
        paid = self.invoice(order='CG-P')
        self.ledger_rows = [paid_row()]
        self.notify(paid)
        self.notify(paid, index='222222', ConfirmationCode='0002222')
        released = self.invoice(order='CG-R')
        _opened(released, 30, order='CG-R')
        self.ledger_down = True
        self.notify(released, index='333333', ConfirmationCode='0003333')
        self.release(released)
        self.ledger_down = False
        lost = self.invoice(order='CG-L')
        _opened(lost, 30, order='CG-L')
        self.ledger_rows = [paid_row(), paid_row(index='222222', approval='0002222'),
                            paid_row(index='333333', approval='0003333')]
        self.day_rows = [row_at('444444', '0004444', 5)]
        for inv in (paid, released, lost):
            unpace(inv)
        sales = StoreSale.objects.count()
        statuses = [self._snapshot(i)[:3] for i in (paid, released, lost)]
        with patch('apps.store.invoice_email.send_store_invoice_email') as mail, \
                patch('apps.store.tranzila_store_invoice.issue_store_tranzila_document') as document:
            self.sweep()
        self.assertEqual(statuses, [self._snapshot(i)[:3] for i in (paid, released, lost)],
                         'no status and no own number changes')
        self.assertEqual(StoreSale.objects.count(), sales, 'nothing sold')
        mail.assert_not_called()
        document.assert_not_called()
        self.assertEqual([c['body'].get('status') for c in self.site.paid_calls].count('failed'), 1,
                         'the one "failed" is the release itself; the sweep told the site nothing')
        # ...and what it learned is kept:
        self.assertEqual(held(paid)['others'], [('222222', 'second_charge', '0002222')])
        self.assertEqual(second_rows(paid).count(), 1)
        self.assertEqual(held(released)['others'][0][:2], ('333333', 'released'))
        self.assertEqual(held(lost)['others'], [('444444', 'suspected', '')])
        # (The found charge may be the released order's new payment as much as the lost one's: told for both.)
        self.assertEqual(sorted(k for k in self.kinds() if k != 'store_possible_double_charge'),
                         ['store_payment_confirmed', 'store_payment_unreported', 'store_payment_unreported',
                          'store_second_charge_confirmed'])


# ---------------------------------------------------------------------------
# 4 — a charge found in the report is kept until a person decides
# ---------------------------------------------------------------------------

class FoundChargeIsKeptTest(Base):
    def _paid_on_second_page(self):
        invoice = self.invoice()
        _opened(invoice, 20, first_minutes=60)
        self.ledger_rows = [paid_row(index='222222', approval='0002222')]
        self.notify(invoice, index='222222', ConfirmationCode='0002222')
        self.assertEqual(self.state(invoice)[0], 'completed')
        self.day_rows = [row_at('111111', '0001111', 40), row_at('222222', '0002222', 1)]
        self.ledger_rows.append(row_at('111111', '0001111', 40))
        return invoice

    def test_F1_a_charge_of_an_earlier_page_is_kept_on_the_paid_invoice(self):
        invoice = self._paid_on_second_page()
        sweep = self.sweep()  # switch off
        self.assertEqual(held(invoice)['others'], [('111111', 'suspected', '')])
        self.assertEqual(sweep['unexplained'], [invoice])
        again = self.sweep()
        self.assertEqual(again['unexplained'], [invoice], 'listed every morning until decided')
        self.assertEqual(self.kinds().count('store_possible_double_charge'), 1, 'told once')
        self.assert_alerts_fit()

    def test_F2_and_told_when_the_order_already_had_a_double_alert(self):
        invoice = self._paid_on_second_page()
        OfficeAlert.objects.create(kind='store_possible_double_charge', dedup_key=f'store_double:{invoice.pk}',
                                   title='x', where='x', what='earlier event')
        self.sweep(complete=True)
        self.assertTrue(OfficeAlert.objects.filter(what__contains='111111').exists())

    def test_F3_an_order_never_looked_for_is_looked_for_after_three_days_too(self):
        invoice = self._paid_on_second_page()
        StoreInvoice.objects.filter(pk=invoice.pk).update(
            payment_page_opened_at=timezone.now() - timedelta(days=4),
            payment_page_first_opened_at=timezone.now() - timedelta(days=4, minutes=40))
        sweep = self.sweep(complete=True)
        self.assertEqual(sweep['unexplained'], [invoice])
        self.assertEqual(held(invoice)['others'], [('111111', 'suspected', '')])

    def test_F3_a_found_charge_stays_listed_after_three_days(self):
        invoice = self._paid_on_second_page()
        self.sweep()
        StoreInvoice.objects.filter(pk=invoice.pk).update(
            payment_page_opened_at=timezone.now() - timedelta(days=4),
            payment_page_first_opened_at=timezone.now() - timedelta(days=4, minutes=40))
        self.assertEqual(self.sweep()['unexplained'], [invoice])
        item = check_stuck_store_payments(date.today())
        self.assertIn('ייתכן חיוב שני', ' '.join(str(row) for row in item.rows))

    def test_V1_a_suspected_charge_on_an_order_paid_by_another_number_stays_listed(self):
        invoice = self.invoice()
        _opened(invoice, 15, first_minutes=40)
        found = row_at('555555', '0005555', 20)
        self.day_rows = [found]
        self.ledger_rows = [found]
        self.assertEqual(self.initiate().status_code, 409)  # tab 1's charge found: suspected
        self.ledger_rows.append(paid_row(index='222222', approval='0002222'))
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='222222', ConfirmationCode='0002222')  # tab 2's notify, confirmed
        self.assertEqual(held(invoice)['others'], [('555555', 'suspected', '')])
        unpace(invoice)
        self.assertEqual(self.sweep(complete=True)['unexplained'], [invoice])

    def test_W1_the_sweep_still_looks_for_a_lost_payment_after_a_release(self):
        invoice = self.invoice()
        _opened(invoice, 60, first_minutes=60)
        self.ledger_down = True
        self.notify(invoice, index='999999', ConfirmationCode='0009999')  # some number, never confirmed
        self.release(invoice)
        self.ledger_down = False
        self.day_rows = []
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_page_opened_at=timezone.now() - timedelta(minutes=40))
        reported_minutes_ago(invoice, 50)
        self.assertIn('iframe_url', self.initiate().json())  # a new page; he pays; notify and return are lost
        charge = row_at('444444', '0004444', 0)
        self.day_rows = [charge]
        self.ledger_rows = [charge]
        unpace(invoice)
        sweep = self.sweep()
        self.assertEqual(sweep['unexplained'], [invoice], 'the charge of the new page is found')
        self.assertIn(('444444', 'suspected', ''), held(invoice)['others'])
        self.assertIn('store_payment_unreported', self.kinds())
        self.assertEqual(self.poll().json()['payment_reported'], True)

    def test_W2_switch_off_a_lost_payment_found_by_the_sweep_is_still_there_after_three_days(self):
        invoice = self.invoice()
        _opened(invoice, 60, first_minutes=60)
        self.day_rows = [row_at('444444', '0004444', 30)]
        first = self.sweep()
        self.assertEqual(first['unexplained'], [invoice])
        self.assertEqual(held(invoice)['others'], [('444444', 'suspected', '')])
        self.assertEqual(self.initiate().status_code, 409, 'kept: the customer gets no second page')
        StoreInvoice.objects.filter(pk=invoice.pk).update(
            payment_page_opened_at=timezone.now() - timedelta(days=4),
            payment_page_first_opened_at=timezone.now() - timedelta(days=4))
        self.assertEqual(self.sweep()['unexplained'], [invoice])

    def test_an_order_leaves_the_search_after_one_complete_read_a_day_after_its_last_page(self):
        invoice = self.invoice()
        _opened(invoice, 60, first_minutes=60)
        self.day_rows = []
        with patch('apps.store.payment_followup._day_report', wraps=payment_followup._day_report) as report:
            self.sweep()
            self.sweep()
            self.assertEqual(report.call_count, 2, 'looked for every morning while a payment may still appear')
            StoreInvoice.objects.filter(pk=invoice.pk).update(
                payment_page_opened_at=timezone.now() - timedelta(hours=25),
                payment_page_first_opened_at=timezone.now() - timedelta(hours=25),
                payment_search_done_at=timezone.now() - timedelta(hours=20))  # the last look: five hours after the page
            self.sweep()
            self.assertEqual(report.call_count, 3)
            self.sweep()
            self.assertEqual(report.call_count, 3, 'one complete read a day after the last page is final')


class DeclineWithFoundChargeTest(Base):
    def test_I1_the_site_is_not_told_failed_while_a_found_charge_holds_the_order(self):
        invoice = self.invoice()
        _opened(invoice, 15)
        self.day_rows = [row_at('555555', '0005555', 3)]
        self.ledger_rows = list(self.day_rows)
        self.assertEqual(self.initiate().status_code, 409)  # suspected kept
        # A notify with a number the report shows declined, then a "declined" notify.
        self.ledger_rows.append({**paid_row(index='123456'), 'processor_response_code': '033'})
        self.notify(invoice)
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, Response='033', index='', ConfirmationCode='')
        self.assertNotIn('failed', [c['body'].get('status') for c in self.site.paid_calls])
        state = held(invoice)
        self.assertEqual((state['status'], state['own']), ('pending', ''))
        self.assertIn(('123456', 'rejected', '0001234'), state['others'])
        self.assertTrue(self.poll().json()['payment_reported'])


# ---------------------------------------------------------------------------
# Alerts: once per order and event — and again after a release
# ---------------------------------------------------------------------------

class AlertAfterReleaseTest(Base):
    def test_G1_a_second_stuck_payment_on_the_same_order_reaches_the_office(self):
        invoice = self.invoice()
        _opened(invoice, 30)
        self.ledger_down = True
        self.notify(invoice, index='999999', ConfirmationCode='0009999')
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_reported_at=timezone.now() - timedelta(minutes=20))
        unpace(invoice)
        with self.captureOnCommitCallbacks(execute=True):
            self.poll()
            unpace(invoice)
            self.poll()
        self.assertEqual(self.kinds().count('store_payment_stuck'), 1, 'once per event')
        self.release(invoice)
        # The customer pays again; the report is still down; twenty minutes pass.
        self.notify(invoice, index='888888', ConfirmationCode='0008888')
        StoreInvoice.objects.filter(pk=invoice.pk).update(payment_reported_at=timezone.now() - timedelta(minutes=20))
        unpace(invoice)
        with self.captureOnCommitCallbacks(execute=True):
            self.poll()
        self.assertEqual(self.kinds().count('store_payment_stuck'), 2)


# ---------------------------------------------------------------------------
# "סגור — לא שלנו": a number that will never be decided, on a paid order
# ---------------------------------------------------------------------------

class CloseNumberTest(Base):
    def _paid_with_a_made_up_number(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        self.ledger_rows = [paid_row()]
        self.notify(invoice)
        with self.captureOnCommitCallbacks(execute=True):
            self.notify(invoice, index='424242', ConfirmationCode='0000001')  # made up; never in the report
        return invoice

    def _paid_on_a_second_page_with_a_found_charge(self):
        invoice = self.invoice(age=timedelta(hours=2))  # the order was opened before its first page
        _opened(invoice, 20, first_minutes=60)
        self.ledger_rows = [paid_row(index='222222', approval='0002222')]
        self.notify(invoice, index='222222', ConfirmationCode='0002222')
        earlier = row_at('111111', '0001111', 40)
        self.day_rows = [earlier]
        self.ledger_rows.append(earlier)
        self.sweep()
        return invoice

    def test_H1_a_number_the_report_never_lists_can_be_closed(self):
        invoice = self._paid_with_a_made_up_number()
        self.assertEqual(self.sweep(complete=True)['second_open'], [invoice])
        self.assertEqual(self.review(invoice, 'close', 'ab').status_code, 400, 'a reason is required')
        # Round 6: not while the report may simply not list it yet (its first ten minutes).
        self.assertEqual(self.review(invoice, 'close', 'מספר מומצא, אין עסקה כזאת בטרנזילה').status_code, 409)
        reported_minutes_ago(invoice, 20)
        res = self.review(invoice, 'close', 'מספר מומצא, אין עסקה כזאת בטרנזילה')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['outcome'], 'closed')
        entry = StoreInvoice.objects.get(pk=invoice.pk).other_transactions[0]
        self.assertEqual((entry['index'], entry['state']), ('424242', 'closed'))
        self.assertEqual(entry['close_reason'], 'מספר מומצא, אין עסקה כזאת בטרנזילה')
        self.assertTrue(entry['closed_by'])
        log = StoreInvoice.objects.get(pk=invoice.pk).payment_review_log[-1]
        self.assertEqual((log['action'], log['numbers'], log['outcome']), ('close', ['424242'], 'closed'))
        unpace(invoice)
        before = len(self.report_calls)
        sweep = self.sweep(complete=True)
        self.assertEqual(sweep['second_open'], [])
        self.assertEqual(self.report_calls[before:], [], 'a closed number is not asked about again')
        self.assertEqual(self.state(invoice), ('completed', 1, 8))

    def test_a_number_the_report_confirms_is_a_second_charge_not_closed(self):
        invoice = self._paid_with_a_made_up_number()
        self.ledger_rows.append(paid_row(index='424242', approval='0000001'))  # it was a real charge
        res = self.review(invoice, 'close', 'נראה מומצא')
        self.assertEqual(res.json()['outcome'], 'second_charge')
        self.assertEqual(held(invoice)['others'], [('424242', 'second_charge', '0000001')])
        self.assertEqual(second_rows(invoice).count(), 1)

    def test_nothing_is_closed_while_the_report_cannot_be_asked(self):
        invoice = self._paid_with_a_made_up_number()
        self.ledger_down = True
        res = self.review(invoice, 'close', 'נראה מומצא')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(held(invoice)['others'], [('424242', 'open', '0000001')])

    def test_close_is_for_a_paid_order_only_and_managers_only(self):
        invoice = self.invoice()
        self.ledger_down = True
        self.notify(invoice)
        res = self.review(invoice, 'close', 'אין חיוב')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(held(invoice)['own'], '123456')
        from apps.core.models import UserProfile
        worker, _user = _manager_client(role=UserProfile.ROLE_WORKER, email='worker@example.com')
        self.assertEqual(worker.post(review_url(invoice), {'action': 'close', 'reason': 'אין חיוב'},
                                     format='json').status_code, 403)

    def test_a_found_charge_on_a_paid_order_is_decided_either_way(self):
        invoice = self._paid_on_a_second_page_with_a_found_charge()
        self.assertEqual(held(invoice)['others'], [('111111', 'suspected', '')])
        # With the customer's evidence the report ties it to the order: a second charge.
        self.assertEqual(self.review(invoice, 'complete', 'הלקוח שילם פעמיים').status_code, 409, 'evidence is needed')
        res = self.review(invoice, 'complete', 'הלקוח שילם פעמיים', confirmation_code='0001111')
        self.assertEqual((res.status_code, res.json()['outcome']), (200, 'second_charge'))
        self.assertEqual(held(invoice)['others'], [('111111', 'second_charge', '0001111')])
        self.assertEqual(second_rows(invoice).count(), 1)
        self.assertEqual(self.state(invoice), ('completed', 1, 8), 'nothing is sold again')
        self.assertEqual(self.sweep()['unexplained'], [])

    def test_a_found_charge_on_a_paid_order_closed_as_not_ours(self):
        invoice = self._paid_on_a_second_page_with_a_found_charge()
        # Round 6: the report shows an approved charge of this sum under the
        # number, so the manager is shown that first, and closes knowingly.
        res = self.review(invoice, 'close', 'של לקוח של האתר השני')
        self.assertEqual((res.status_code, res.json()['outcome']), (409, 'charge_shown'))
        res = self.review(invoice, 'close', 'של לקוח של האתר השני', acknowledge_charge=['111111'])
        self.assertEqual((res.status_code, res.json()['outcome']), (200, 'closed'))
        self.assertEqual(held(invoice)['others'], [('111111', 'closed', '')])
        self.assertEqual(self.sweep()['unexplained'], [], 'decided: no longer listed, and not found again')
        self.assertEqual(second_rows(invoice).count(), 0)


# ---------------------------------------------------------------------------
# The shape of a number
# ---------------------------------------------------------------------------

class NumberShapeTest(Base):
    def test_L1_a_very_long_number_is_refused_not_an_error(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        self.client.raise_request_exception = False
        res = self.notify(invoice, index='9' * 150, ConfirmationCode='1')
        self.assertLess(res.status_code, 500)
        self.assertEqual(held(invoice), {'status': 'pending', 'own': '', 'code': '', 'others': []})
        self.assertEqual(self.returned('9' * 150).status_code, 400)
        self.assertEqual(self.returned('0').status_code, 400)
        self.assertEqual(self.report_calls, [])

    def test_L2_a_leading_zero_is_the_same_number(self):
        invoice = self.invoice()
        _opened(invoice, 5)
        self.ledger_rows = [paid_row()]
        self.notify(invoice, index='0123456')  # the real number with a zero in front
        self.assertEqual(self.report_calls, ['123456'])
        self.assertEqual(self.state(invoice), ('completed', 1, 8))
        self.notify(invoice)
        self.assertEqual(self.returned('00123456', '0001234').status_code, 200)
        self.assertEqual(held(invoice)['others'], [])
        self.assertEqual(held(invoice)['own'], '123456')
