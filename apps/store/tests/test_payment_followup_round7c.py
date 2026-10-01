"""
Stage 3, seventh round, part 3 (probes K2, K3 of reviewtests6): "I saw the
charge in the report" counts only for a number the server has already shown,
named in the request — and an answer that closed some numbers still says
which one shows a charge. Plus what the office reads while a retry waits.
"""
from datetime import timedelta

from django.utils import timezone

from apps.store.models import StoreInvoice
from apps.store.serializers import StoreInvoiceSerializer
from apps.store.tests.test_payment_followup import paid_row
from apps.store.tests.test_payment_followup_round4 import _opened
from apps.store.tests.test_payment_followup_round5 import Base, held


class AcknowledgeTheShownNumberTest(Base):
    def _paid_with_others(self, *indexes):
        invoice = self.invoice()
        _opened(invoice, 40)
        self.ledger_rows = [paid_row(index='777777', approval='0007777')]
        self.notify(invoice, index='777777', ConfirmationCode='0007777')
        self.assertEqual(self.state(invoice)[:2], ('completed', 1))
        old = (timezone.now() - timedelta(minutes=30)).isoformat()
        StoreInvoice.objects.filter(pk=invoice.pk).update(other_transactions=[
            {'index': index, 'code': '', 'terminal': 'iframe_terminal', 'reported_at': old, 'state': 'open'}
            for index in indexes])
        return invoice

    def states(self, invoice):
        return {index: state for index, state, _code in held(invoice)['others']}

    def test_K2_a_tick_given_in_advance_closes_nothing(self):
        invoice = self._paid_with_others('888')
        self.ledger_rows.append(paid_row(index='888', approval='0000888'))
        manager = self.manager()
        # A general flag, and even the number itself before the server showed it: the first press shows the charge.
        for early in (True, ['888']):
            res = self.review(invoice, 'close', 'לא שלנו', manager=manager, acknowledge_charge=early)
            self.assertEqual((res.status_code, res.json()['outcome']), (409, 'charge_shown'), early)
            self.assertEqual(res.json()['charge_shown'], ['888'])
            self.assertEqual(self.states(invoice), {'888': 'open'})
            StoreInvoice.objects.filter(pk=invoice.pk).update(other_transactions=[
                {k: v for k, v in e.items() if k != 'charge_shown_at'}
                for e in StoreInvoice.objects.get(pk=invoice.pk).other_transactions])
        # Shown now; a tick that names another number, or none, still closes nothing.
        self.review(invoice, 'close', 'לא שלנו', manager=manager)
        for wrong in (True, ['889'], []):
            res = self.review(invoice, 'close', 'לא שלנו', manager=manager, acknowledge_charge=wrong)
            self.assertEqual(res.status_code, 409, wrong)
        res = self.review(invoice, 'close', 'של לקוח אחר', manager=manager, acknowledge_charge=['888'])
        self.assertEqual((res.status_code, res.json()['outcome']), (200, 'closed'))
        entry = StoreInvoice.objects.get(pk=invoice.pk).other_transactions[0]
        self.assertEqual((entry['state'], entry['closed_over_charge']), ('closed', True))
        self.assertTrue(entry['charge_shown_at'])
        self.assertIn('888', StoreInvoice.objects.get(pk=invoice.pk).payment_review_log[-1]['evidence'])

    def test_K3_some_closed_and_one_shown_the_answer_says_so(self):
        invoice = self._paid_with_others('888', '889')
        self.ledger_rows.append(paid_row(index='888', approval='0000888'))  # 889 is in no report
        res = self.review(invoice, 'close', 'לא שלנו')
        body = res.json()
        self.assertEqual((res.status_code, body['outcome']), (200, 'closed'))
        self.assertEqual(self.states(invoice), {'888': 'open', '889': 'closed'})
        self.assertEqual(body['charge_shown'], ['888'])
        self.assertIn('888', body['warning'])
        self.assertIn('חיוב מאושר', body['warning'])

    def test_nothing_to_warn_about_carries_no_warning(self):
        invoice = self._paid_with_others('889')
        body = self.review(invoice, 'close', 'מספר מומצא').json()
        self.assertEqual(body['outcome'], 'closed')
        self.assertNotIn('warning', body)
        self.assertNotIn('charge_shown', body)


class OfficeSeesTheWaitTest(Base):
    def test_the_invoice_says_so_while_a_retry_is_refused_and_stops_when_it_ends(self):
        invoice = self.invoice()
        self.assertFalse(StoreInvoiceSerializer(invoice).data['payment_retry_waiting'])
        _opened(invoice, 45, first_minutes=45)
        self.day_report_down = True
        self.assertEqual(self.initiate().status_code, 409)
        invoice.refresh_from_db()
        self.assertTrue(StoreInvoiceSerializer(invoice).data['payment_retry_waiting'])
        self.assertFalse(StoreInvoiceSerializer(invoice).data['payment_in_review'])
        self.day_report_down = False
        self.assertIn('iframe_url', self.initiate().json())
        invoice.refresh_from_db()
        self.assertFalse(StoreInvoiceSerializer(invoice).data['payment_retry_waiting'])
