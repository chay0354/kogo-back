"""
Drafts of a receipt (RC) and an invoice-receipt (IRM) — audit M11, the owner's
request of 25.9.2026 — with the guarantees WS-2 built for drafts (F):

- a draft has no fiscal number, is never signed, pays nothing and is in no
  report, export, run or open balance;
- it carries its payment rows (built as a direct issue builds them) and the
  invoices it will settle, checked when it is saved;
- approval locks it, checks the payments again, takes the run's next number
  and today's date, and records the settlements against the balances of that
  moment — a refusal rolls it all back and uses no number;
- signing and delivery are the direct issue's.
"""
import threading
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.core.tranzila_ledger import list_ledger_documents
from apps.documents import service, settlement
from apps.documents.models import DocumentPayment, DocumentSeries, DocumentSettlement, FormalDocument, SignedOriginal
from apps.documents.numbering import continuity
from apps.documents.period_report import scoped_documents
from apps.documents.tests.signing_support import signing_on
from apps.documents.tests.test_drafts_and_pdf import make_user
from apps.documents.tests.test_settlements import SettlementFixture, irm_payload, receipt_payload

CREATE = '/api/v1/documents/documents/create-document/'
DETAIL = '/api/v1/documents/documents/{}/'
FINALIZE = '/api/v1/documents/documents/{}/finalize/'
DISCARD = '/api/v1/documents/documents/{}/discard/'
OPEN = '/api/v1/documents/documents/open-invoices/'


def as_draft(payload, target):
    """The same payload, saved as a draft of `target`."""
    return {**payload, 'document_type': 'draft', 'draft_target_type': target}


def check_receipt_payload(child, *, day, crossed, amount='1180.00'):
    return {
        'document_type': 'receipt',
        'client_type': 'existing',
        'child_id': str(child.id),
        'document_date': str(day),
        'receipt_details': {
            'payment_method': "צ'ק",
            'checks': [{
                'amount': float(amount), 'confirmed': True, 'date': str(day), 'check_number': '000123',
                'bank': '12', 'branch': '600', 'account_number': '456789', 'check_crossed': crossed,
            }],
        },
    }


class PayingDraftFixture(SettlementFixture):
    def setUp(self):
        super().setUp()
        self.manager = make_user('paying-draft-manager@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)

    def draft(self, payload, target):
        res = self.client.post(CREATE, as_draft(payload, target), format='json')
        self.assertEqual(res.status_code, 201, res.data)
        return FormalDocument.objects.get(pk=res.data['id'])

    def receipt_draft(self, amount='1180.00', settlements=None, **kw):
        return self.draft(receipt_payload(self.kid, amount, day=self.today, settlements=settlements, **kw), 'receipt')

    def approve(self, doc):
        return self.client.post(FINALIZE.format(doc.pk))


@override_settings(TRANZILA_BILLING_TERMINAL='')
class SavingADraftTests(PayingDraftFixture, APITestCase):
    def test_a_receipt_draft_keeps_its_payments_and_takes_no_number(self):
        ti = self.invoice(f'TI-{self.year}-000900')
        draft = self.receipt_draft(settlements=[{'invoice_id': str(ti.pk), 'amount': '1180.00'}])

        self.assertEqual((draft.document_type, draft.draft_target_type), ('draft', 'receipt'))
        self.assertTrue(draft.document_number.startswith('D-'))
        self.assertIsNone(draft.issued_at)
        self.assertEqual(draft.total_amount, Decimal('1180.00'))
        self.assertEqual(
            list(draft.payments.values_list('payment_method', 'amount')), [('cash', Decimal('1180.00'))],
        )
        self.assertEqual(self.counter('RC'), 0)
        # What it will settle is kept on the draft — not written as a settlement.
        self.assertEqual(draft.draft_settlements, [
            {'invoice_id': str(ti.pk), 'invoice_number': ti.document_number, 'amount': '1180.00'},
        ])
        self.assertFalse(DocumentSettlement.objects.exists())

    def test_a_draft_pays_nothing_until_it_is_approved(self):
        ti = self.invoice(f'TI-{self.year}-000900')
        self.receipt_draft(settlements=[{'invoice_id': str(ti.pk), 'amount': '1180.00'}])

        self.assertEqual(settlement.balance_of(ti).open, Decimal('1180.00'))
        picker = self.client.get(OPEN, {'child_id': str(self.kid.id)}).data
        self.assertEqual([row['document_number'] for row in picker['results']], [ti.document_number])
        # Another receipt may still pay it.
        paid = self.client.post(CREATE, receipt_payload(
            self.kid, '1180.00', day=self.today, settlements=[{'invoice_id': str(ti.pk), 'amount': '1180.00'}],
        ), format='json')
        self.assertEqual(paid.status_code, 201, paid.data)

    def test_an_invoice_receipt_draft_keeps_its_lines_payments_and_withholding(self):
        payload = irm_payload(self.kid, '1000.00', day=self.today)
        payload['invoice_details']['payments'] = [
            {'method': 'מזומן', 'amount': '500.00'},
            {'method': "צ'ק", 'amount': '650.00', 'check_number': '77', 'check_date': str(self.today)},
        ]
        payload['invoice_details']['withholding_amount'] = '30.00'
        draft = self.draft(payload, 'combined')

        self.assertEqual((draft.document_type, draft.draft_target_type), ('draft', 'combined'))
        self.assertEqual((draft.subtotal, draft.vat_amount, draft.total_amount),
                         (Decimal('1000.00'), Decimal('180.00'), Decimal('1180.00')))
        self.assertEqual(draft.withholding_amount, Decimal('30.00'))
        self.assertEqual(draft.line_items.count(), 1)
        self.assertEqual(sorted(draft.payments.values_list('payment_method', 'amount')),
                         [('cash', Decimal('500.00')), ('check', Decimal('650.00'))])
        self.assertEqual(self.counter('IRM'), 0)

    def test_payments_that_do_not_add_up_are_refused_when_the_draft_is_saved(self):
        payload = irm_payload(self.kid, '1000.00', day=self.today)
        payload['invoice_details']['payments'] = [{'method': 'מזומן', 'amount': '1000.00'}]
        res = self.client.post(CREATE, as_draft(payload, 'combined'), format='json')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(FormalDocument.objects.exists())

    def test_a_receipt_draft_with_nothing_received_is_refused(self):
        res = self.client.post(CREATE, as_draft(receipt_payload(self.kid, '0', day=self.today), 'receipt'),
                               format='json')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(FormalDocument.objects.exists())

    def test_a_settlement_above_what_is_open_is_refused_when_the_draft_is_saved(self):
        ti = self.invoice(f'TI-{self.year}-000900', total='500.00')
        res = self.client.post(CREATE, as_draft(receipt_payload(
            self.kid, '1180.00', day=self.today, settlements=[{'invoice_id': str(ti.pk), 'amount': '600.00'}],
        ), 'receipt'), format='json')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('נותרו לתשלום', res.data['error'])
        self.assertFalse(FormalDocument.objects.filter(document_type='draft').exists())

    def test_a_receipt_draft_settles_tax_invoices_only(self):
        tx = self.invoice(f'TX-{self.year}-000900', kind='transaction_invoice')
        res = self.client.post(CREATE, as_draft(receipt_payload(
            self.kid, '1180.00', day=self.today, settlements=[{'invoice_id': str(tx.pk), 'amount': '1180.00'}],
        ), 'receipt'), format='json')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('חשבונית עסקה', res.data['error'])

    def test_settlements_on_an_invoice_draft_are_refused(self):
        ti = self.invoice(f'TI-{self.year}-000900')
        payload = self.invoice_payload('tax_invoice', self.today)
        payload['settlements'] = [{'invoice_id': str(ti.pk), 'amount': '100.00'}]
        res = self.client.post(CREATE, as_draft(payload, 'tax_invoice'), format='json')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('settlements', res.data)

    def test_invoice_per_check_is_not_saved_as_a_draft(self):
        payload = check_receipt_payload(self.kid, day=self.today, crossed=True)
        payload['receipt_details']['invoice_per_check'] = True
        res = self.client.post(CREATE, as_draft(payload, 'receipt'), format='json')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('invoice_per_check', res.data['receipt_details'])

    def test_a_draft_carries_no_allocation_number(self):
        payload = irm_payload(self.kid, '1000.00', day=self.today)
        payload['invoice_details']['allocation_number'] = '123456789'
        res = self.client.post(CREATE, as_draft(payload, 'combined'), format='json')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('טיוטה', str(res.data))

    def test_a_draft_is_in_no_report_run_or_export_and_owes_nothing_on_the_ledger(self):
        draft = self.receipt_draft()

        self.assertNotIn(draft.pk, scoped_documents(self.manager, self.today, self.today)[0].values_list('pk', flat=True))
        run = next((r for r in continuity(self.year) if r.series == 'RC'), None)
        self.assertTrue(run is None or run.issued == 0)
        rows = [row for row in list_ledger_documents(start_date=self.today, end_date=self.today, local_only=True)['documents']
                if row['id'] == str(draft.pk)]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['status'], row['amount_paid'], row['open_balance'], row['applied_amount']),
                         ('draft', 0.0, 0.0, 0.0))
        self.assertEqual((row['is_draft'], row['draft_target_type']), (True, 'receipt'))

    def test_the_detail_shows_what_the_draft_will_settle(self):
        ti = self.invoice(f'TI-{self.year}-000900')
        draft = self.receipt_draft(settlements=[{'invoice_id': str(ti.pk), 'amount': '1180.00'}])
        detail = self.client.get(DETAIL.format(draft.pk)).data
        self.assertEqual(detail['draft_settlements'][0]['invoice_number'], ti.document_number)
        self.assertEqual(detail['settles'], [])


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class ApprovingADraftTests(PayingDraftFixture, APITestCase):
    def test_an_approved_receipt_takes_the_next_number_today_and_settles_then(self, _mail):
        ti = self.invoice(f'TI-{self.year}-000900')
        draft = self.receipt_draft(settlements=[{'invoice_id': str(ti.pk), 'amount': '1180.00'}])
        before = timezone.now()

        res = self.approve(draft)

        self.assertEqual(res.status_code, 200, res.data)
        doc = FormalDocument.objects.get(pk=draft.pk)
        self.assertEqual((doc.document_type, doc.document_number), ('receipt', f'RC-{self.year}-000001'))
        self.assertEqual(doc.document_date, self.today)
        self.assertGreaterEqual(doc.issued_at, before)
        self.assertEqual(doc.issued_by, self.manager)
        self.assertIsNone(doc.draft_settlements)
        row = DocumentSettlement.objects.get()
        self.assertEqual((row.payer_id, row.invoice_id, row.amount, row.created_by),
                         (doc.pk, ti.pk, Decimal('1180.00'), self.manager))
        self.assertEqual(settlement.balance_of(ti).status, 'paid')
        self.assertEqual([line['document_number'] for line in res.data['settles']], [ti.document_number])

    def test_an_approved_draft_is_dated_the_day_it_is_approved(self, _mail):
        typed = self.today - timedelta(days=3) if self.today.day > 3 else self.today
        draft = self.draft(receipt_payload(self.kid, '100.00', day=typed), 'receipt')
        self.approve(draft)
        self.assertEqual(FormalDocument.objects.get(pk=draft.pk).document_date, self.today)

    def test_an_invoice_that_was_paid_meanwhile_refuses_the_approval_and_uses_no_number(self, _mail):
        ti = self.invoice(f'TI-{self.year}-000900')
        draft = self.receipt_draft(settlements=[{'invoice_id': str(ti.pk), 'amount': '1180.00'}])
        # Another receipt pays the invoice after the draft was saved.
        paid = self.client.post(CREATE, receipt_payload(
            self.kid, '500.00', day=self.today, settlements=[{'invoice_id': str(ti.pk), 'amount': '500.00'}],
        ), format='json')
        self.assertEqual(paid.status_code, 201, paid.data)
        counter = self.counter('RC')

        res = self.approve(draft)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('נותרו לתשלום', res.data['error'])
        still = FormalDocument.objects.get(pk=draft.pk)
        self.assertEqual((still.document_type, still.document_number), ('draft', draft.document_number))
        self.assertEqual(still.draft_settlements, draft.draft_settlements)
        self.assertEqual(self.counter('RC'), counter)
        self.assertFalse(DocumentSettlement.objects.filter(payer=draft.pk).exists())
        self.assertEqual(settlement.balance_of(ti).open, Decimal('680.00'))

    def test_a_credit_note_issued_meanwhile_is_counted_at_approval(self, _mail):
        ti = self.invoice(f'TI-{self.year}-000900')
        draft = self.receipt_draft(settlements=[{'invoice_id': str(ti.pk), 'amount': '1180.00'}])
        credit = self.client.post(CREATE, {
            'document_type': 'credit_invoice', 'client_type': 'existing', 'child_id': str(self.kid.id),
            'credit_invoice_details': {
                'document_date': str(self.today), 'linked_invoice_id': ti.document_number,
                'credit_reason': 'הנחה', 'credit_amount_before_vat': '100.00',
            },
        }, format='json')
        self.assertEqual(credit.status_code, 201, credit.data)

        res = self.approve(draft)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(FormalDocument.objects.get(pk=draft.pk).document_type, 'draft')

    def test_payment_rows_are_checked_again_at_approval(self, _mail):
        draft = self.receipt_draft(amount='300.00')
        DocumentPayment.objects.filter(document=draft).update(amount=Decimal('200.00'))

        res = self.approve(draft)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(FormalDocument.objects.get(pk=draft.pk).document_type, 'draft')
        self.assertEqual(self.counter('RC'), 0)

    def test_the_withholding_settles_its_part_of_the_invoice(self, _mail):
        ti = self.invoice(f'TI-{self.year}-000900')
        draft = self.receipt_draft(amount='1150.00', withholding='30.00',
                                   settlements=[{'invoice_id': str(ti.pk), 'amount': '1180.00'}])
        res = self.approve(draft)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(settlement.balance_of(ti).open, Decimal('0.00'))

    def test_the_older_forms_linked_number_settles_at_approval_as_on_a_direct_receipt(self, _mail):
        ti = self.invoice(f'TI-{self.year}-000900')
        draft = self.receipt_draft(amount='1180.00', linked=ti.document_number)
        self.assertEqual(settlement.balance_of(ti).open, Decimal('1180.00'))
        self.approve(draft)
        self.assertEqual(DocumentSettlement.objects.get().payer_id, draft.pk)
        self.assertEqual(settlement.balance_of(ti).open, Decimal('0.00'))

    def test_an_approved_invoice_receipt_closes_the_transaction_invoice(self, _mail):
        tx = self.invoice(f'TX-{self.year}-000900', kind='transaction_invoice')
        draft = self.draft(irm_payload(self.kid, '1000.00', day=self.today,
                                       settlements=[{'invoice_id': str(tx.pk), 'amount': '1180.00'}]), 'combined')
        res = self.approve(draft)
        self.assertEqual(res.status_code, 200, res.data)
        doc = FormalDocument.objects.get(pk=draft.pk)
        self.assertEqual((doc.document_type, doc.document_number), ('combined', f'IRM-{self.year}-000001'))
        self.assertEqual(settlement.balance_of(tx).status, 'paid')

    def test_a_second_approval_is_refused(self, _mail):
        draft = self.receipt_draft(amount='100.00')
        first = self.approve(draft)
        second = self.approve(draft)
        self.assertEqual((first.status_code, second.status_code), (200, 400))
        self.assertEqual(self.counter('RC'), 1)

    def test_a_partner_cannot_approve(self, _mail):
        draft = self.receipt_draft(amount='100.00')
        self.client.force_authenticate(make_user('paying-draft-partner@test', UserProfile.ROLE_PARTNER))
        self.assertEqual(self.approve(draft).status_code, 403)
        self.assertEqual(FormalDocument.objects.get(pk=draft.pk).document_type, 'draft')


@override_settings(TRANZILA_BILLING_TERMINAL='')
class DiscardingADraftTests(PayingDraftFixture, APITestCase):
    def test_a_draft_is_deleted_with_its_payments_and_leaves_no_gap(self):
        draft = self.receipt_draft(amount='100.00')
        res = self.client.post(DISCARD.format(draft.pk))
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['document_number'], draft.document_number)
        self.assertFalse(FormalDocument.objects.filter(pk=draft.pk).exists())
        self.assertFalse(DocumentPayment.objects.exists())
        self.assertEqual(self.counter('RC'), 0)

    def test_an_issued_document_is_never_deleted(self):
        draft = self.receipt_draft(amount='100.00')
        self.approve(draft)
        res = self.client.post(DISCARD.format(draft.pk))
        self.assertEqual(res.status_code, 400, res.data)
        self.assertTrue(FormalDocument.objects.filter(pk=draft.pk, document_type='receipt').exists())

    def test_a_partner_cannot_discard(self):
        draft = self.receipt_draft(amount='100.00')
        self.client.force_authenticate(make_user('discard-partner@test', UserProfile.ROLE_PARTNER))
        self.assertEqual(self.client.post(DISCARD.format(draft.pk)).status_code, 403)
        self.assertTrue(FormalDocument.objects.filter(pk=draft.pk).exists())


@signing_on()
class SigningAtApprovalTests(PayingDraftFixture, APITestCase):
    """The original of an approved draft is signed and delivered exactly as a direct issue's."""

    def setUp(self):
        super().setUp()
        self.kid.family.email = 'parent@example.com'
        self.kid.family.save(update_fields=['email'])
        patcher = patch('apps.documents.document_email.send_resend_email', return_value='msg-id')
        self.mail = patcher.start()
        self.addCleanup(patcher.stop)

    def issue_directly(self, payload):
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(CREATE, payload, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        return SignedOriginal.objects.get(number=res.data['document_number'])

    def issue_by_approval(self, payload, target):
        draft = self.draft(payload, target)
        self.assertFalse(SignedOriginal.objects.filter(source_id=str(draft.pk)).exists())
        with self.captureOnCommitCallbacks(execute=True):
            res = self.approve(draft)
        self.assertEqual(res.status_code, 200, res.data)
        return SignedOriginal.objects.get(source_id=str(draft.pk))

    def test_a_crossed_check_receipt_is_signed_and_mailed_as_when_issued_directly(self):
        payload = check_receipt_payload(self.kid, day=self.today, crossed=True, amount='100.00')
        direct = self.issue_directly(payload)
        approved = self.issue_by_approval(payload, 'receipt')

        self.assertTrue(approved.is_signed)
        self.assertEqual(approved.number, FormalDocument.objects.get(pk=approved.source_id).document_number)
        self.assertEqual((approved.channel, approved.delivery, approved.delivery_reason),
                         (direct.channel, direct.delivery, direct.delivery_reason))
        self.assertEqual(approved.delivery, SignedOriginal.DELIVERY_EMAIL)
        self.assertIsNotNone(approved.sent_at)

    def test_cash_goes_on_paper_as_when_issued_directly(self):
        payload = receipt_payload(self.kid, '100.00', day=self.today)
        direct = self.issue_directly(payload)
        approved = self.issue_by_approval(payload, 'receipt')
        self.assertEqual((approved.delivery, approved.delivery_reason), (direct.delivery, direct.delivery_reason))
        self.assertEqual(approved.delivery, SignedOriginal.DELIVERY_PAPER)

    def test_an_invoice_receipt_by_card_is_signed_and_mailed_as_when_issued_directly(self):
        payload = irm_payload(self.kid, '100.00', day=self.today)
        payload['invoice_details']['payments'] = [
            {'method': 'אשראי', 'amount': '118.00', 'card_last_four': '4242', 'card_brand': 'ויזה'},
        ]
        direct = self.issue_directly(payload)
        approved = self.issue_by_approval(payload, 'combined')
        self.assertEqual((approved.channel, approved.delivery, approved.delivery_reason),
                         (direct.channel, direct.delivery, direct.delivery_reason))
        self.assertTrue(approved.is_signed)

    def test_a_refused_approval_records_no_original(self):
        ti = self.invoice(f'TI-{self.year}-000900', total='100.00')
        draft = self.receipt_draft(amount='100.00', settlements=[{'invoice_id': str(ti.pk), 'amount': '100.00'}])
        self.issue_directly(receipt_payload(self.kid, '100.00', day=self.today,
                                            settlements=[{'invoice_id': str(ti.pk), 'amount': '100.00'}]))
        with self.captureOnCommitCallbacks(execute=True):
            res = self.approve(draft)
        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(SignedOriginal.objects.filter(source_id=str(draft.pk)).exists())


@override_settings(TRANZILA_BILLING_TERMINAL='')
class TwoApprovalsOfAReceiptDraftTests(TransactionTestCase):
    """The race of WS-2's test_draft_approval_race, on a receipt draft that settles an invoice."""

    serialized_rollback = True

    def run_together(self, *jobs):
        start = threading.Barrier(len(jobs))
        results = []

        def run(job):
            try:
                start.wait(timeout=10)
                results.append(('ok', job()))
            except ValueError as exc:
                results.append(('refused', str(exc)))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(job,)) for job in jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results

    def setUp(self):
        self.fixture = SettlementFixture()
        self.fixture.setUp()
        self.manager = make_user('paying-race@test', UserProfile.ROLE_MANAGER)
        self.ti = self.fixture.invoice(f'TI-{self.fixture.year}-000900')
        data = receipt_payload(self.fixture.kid, '1180.00', day=self.fixture.today,
                               settlements=[{'invoice_id': str(self.ti.pk), 'amount': '1180.00'}])
        self.draft = service.create_draft({**data, 'document_type': 'draft', 'draft_target_type': 'receipt'})

    def approve(self):
        # Read before the barrier, as the view's get_object() does: both copies say "draft".
        stale = FormalDocument.objects.get(pk=self.draft.pk)
        return lambda: service.finalize_draft(stale, issued_by=self.manager).document_number

    def assert_one_receipt_one_settlement(self):
        year = self.fixture.year
        self.assertEqual(DocumentSeries.objects.get(series='RC', year=year).counter, 1)
        run = next(r for r in continuity(year) if r.series == 'RC')
        self.assertEqual((run.issued, run.missing), (1, ()))
        self.assertEqual(DocumentSettlement.objects.filter(invoice=self.ti, voided_at__isnull=True).count(), 1)
        self.assertEqual(settlement.balance_of(self.ti).paid, Decimal('1180.00'))

    def test_two_approvals_of_one_draft_issue_one_receipt_and_settle_once(self):
        results = self.run_together(self.approve(), self.approve())

        self.assertEqual(sorted(kind for kind, _ in results), ['ok', 'refused'], results)
        doc = FormalDocument.objects.get(pk=self.draft.pk)
        self.assertEqual(doc.document_type, 'receipt')
        self.assertEqual(FormalDocument.objects.filter(document_type='receipt').count(), 1)
        self.assert_one_receipt_one_settlement()

    def test_an_approval_and_a_direct_receipt_for_the_same_invoice_pay_it_once(self):
        def direct():
            from django.db import transaction

            with transaction.atomic():
                doc = service.create_receipt(receipt_payload(
                    self.fixture.kid, '1180.00', day=self.fixture.today), issued_by=self.manager)
                settlement.record_settlements(doc, [{'invoice_id': self.ti.pk, 'amount': '1180.00'}], user=self.manager)
            return doc.document_number

        results = self.run_together(self.approve(), direct)

        self.assertEqual(sorted(kind for kind, _ in results), ['ok', 'refused'], results)
        self.assert_one_receipt_one_settlement()
        # Whichever lost left nothing: a refused approval is still a draft, a refused receipt was never written.
        self.assertEqual(FormalDocument.objects.filter(document_type='receipt').count(), 1)
        self.assertEqual(
            FormalDocument.objects.filter(pk=self.draft.pk, document_type__in=('draft', 'receipt')).count(), 1,
        )
