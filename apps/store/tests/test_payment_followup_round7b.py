"""
Stage 3, seventh round, part 2 (probes L1-L3 of reviewtests6): a website
order from before the follow-up — a transaction number on it, no
payment_reported_at — gets no payment page. A payment made on it would land
beside a number nobody can ask the report about, and nothing could complete
the order (the probes showed exactly that). The site is told to open a new
order, and what it reads in the status agrees with that.
"""
from datetime import timedelta

from django.utils import timezone

from apps.store.models import StoreInvoice
from apps.store.tests.test_payment_followup_review import ORDER
from apps.store.tests.test_payment_followup_round5 import Base, held


def legacy(test, *, status='pending', txn='41', terminal='', order=ORDER, code=''):
    """A website order from before the follow-up: a number on it, no payment_reported_at, its cart kept."""
    invoice = test.invoice(order=order, status=status)
    StoreInvoice.objects.filter(pk=invoice.pk).update(
        tranzila_transaction_id=txn, tranzila_terminal=terminal, tranzila_confirmation_code=code,
        payment_reported_at=None, created_at=timezone.now() - timedelta(days=12),
        website_idempotency_key=f'idemp-{order}',
    )
    invoice.refresh_from_db()
    return invoice


class OldOrderGetsNoPageTest(Base):
    def assert_refused_untouched(self, invoice, status):
        res = self.initiate()
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(res.json()['error'], 'ההזמנה הזאת ישנה — צרו הזמנה חדשה')
        self.assertNotIn('iframe_url', res.json())
        self.assertEqual(held(invoice), {'status': status, 'own': '41', 'code': '', 'others': []})
        fresh = StoreInvoice.objects.get(pk=invoice.pk)
        self.assertEqual((fresh.payment_page_opened_at, fresh.payment_retry_refused_at, fresh.payment_followup_at),
                         (None, None, None), 'nothing is written')
        self.assertEqual((self.report_calls, self.kinds(), self.site.paid_calls), ([], [], []))

    def test_L1_an_old_pending_order_with_a_number_gets_no_page(self):
        self.assert_refused_untouched(legacy(self, status='pending', terminal=''), 'pending')

    def test_L1b_whichever_terminal_its_number_was_on(self):
        self.assert_refused_untouched(legacy(self, status='pending', terminal='realtest'), 'pending')

    def test_L3_an_old_failed_order_reads_failed_and_a_retry_is_told_to_open_a_new_order(self):
        invoice = legacy(self, status='failed', terminal='realtest')
        poll = self.poll().json()
        # Not "wait" against "failed", and not a page: the site reads failed,
        # and its retry gets 400 — on which it opens a new order.
        self.assertEqual((poll['status'], poll['payment_reported'], poll['paid']), ('failed', False, False))
        self.assert_refused_untouched(invoice, 'failed')
        self.assertEqual(self.poll().json(), poll, 'the refusal changes nothing the site reads')

    def test_an_old_pending_order_reads_pending_with_no_payment_reported(self):
        legacy(self, status='pending')
        poll = self.poll().json()
        self.assertEqual((poll['status'], poll['payment_reported'], poll['paid']), ('pending', False, False))

    def test_an_order_with_no_number_on_it_is_not_old(self):
        invoice = self.invoice(status='failed')
        StoreInvoice.objects.filter(pk=invoice.pk).update(
            created_at=timezone.now() - timedelta(days=12), website_idempotency_key=f'idemp-{ORDER}')
        self.assertIn('iframe_url', self.initiate().json(), 'an old order nobody paid on may still be paid')

    def test_an_order_reported_under_the_follow_up_is_not_old(self):
        invoice = self.invoice()
        StoreInvoice.objects.filter(pk=invoice.pk).update(website_idempotency_key=f'idemp-{ORDER}')
        self.ledger_down = True
        self.notify(invoice)  # in review, with payment_reported_at
        self.assertEqual(self.initiate().status_code, 409)
