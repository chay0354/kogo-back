"""
Receipts against invoices (C): which receipt paid which invoice, and what each
invoice still owes — on the document, in the picker, on the collections tab,
on the business customer's card and in the dashboard's totals.
"""
import threading
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.db import connections
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.core.tranzila_ledger import list_ledger_documents
from apps.customers.models import Child, Family
from apps.documents import settlement
from apps.documents.document_pdf import build_document_layout
from apps.documents.models import DocumentSeries, DocumentSettlement, FormalDocument
from apps.documents.tests.test_document_dates import Fixture
from apps.documents.tests.test_drafts_and_pdf import make_user
from apps.rentals.tests.factories import make_branch, make_customer
from apps.rentals.tests.factories import make_user as make_scoped_user

CREATE = '/api/v1/documents/documents/create-document/'
OPEN = '/api/v1/documents/documents/open-invoices/'
DETAIL = '/api/v1/documents/documents/{}/'
VOID = '/api/v1/documents/settlements/{}/void/'


def receipt_payload(child, amount, *, day, settlements=None, linked='', withholding='0'):
    payload = {
        'document_type': 'receipt',
        'client_type': 'existing',
        'child_id': str(child.id),
        'document_date': str(day),
        'receipt_details': {
            'payment_method': 'מזומן', 'cash_amount': str(amount),
            'linked_invoice_id': linked, 'withholding': withholding,
        },
    }
    if settlements is not None:
        payload['settlements'] = settlements
    return payload


def irm_payload(child, amount, *, day, settlements=None):
    """An invoice-receipt for `amount` before VAT, paid in cash."""
    gross = (Decimal(amount) * Decimal('1.18')).quantize(Decimal('0.01'))
    payload = {
        'document_type': 'combined',
        'client_type': 'existing',
        'child_id': str(child.id),
        'invoice_details': {
            'document_date': str(day),
            'line_items': [{'description': 'תשלום', 'quantity': 1, 'price': str(amount)}],
            'payments': [{'method': 'מזומן', 'amount': str(gross)}],
        },
    }
    if settlements is not None:
        payload['settlements'] = settlements
    return payload


class SettlementFixture(Fixture):
    def invoice(self, number, total='1180.00', kind='tax_invoice', child=None, day=None, **fields):
        total = Decimal(total)
        return FormalDocument.objects.create(
            document_number=number, document_type=kind, client_type='existing', child=child or self.kid,
            document_date=day or self.today, subtotal=(total / Decimal('1.18')).quantize(Decimal('0.01')),
            vat_amount=total - (total / Decimal('1.18')).quantize(Decimal('0.01')), total_amount=total, **fields,
        )

    def other_kid(self):
        return Child.objects.create(
            family=Family.objects.create(name='אחרת'), first_name='דן', last_name='לוי',
            birth_date=date(2014, 1, 1), gender='male', status='active',
        )


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class ReceiptPaysInvoiceTests(SettlementFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_user('settle-manager@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)
        self.ti = self.invoice(f'TI-{self.year}-000900')

    def pay(self, amount, rows, **kw):
        return self.client.post(CREATE, receipt_payload(self.kid, amount, day=self.today, settlements=rows, **kw),
                                format='json')

    def test_a_receipt_closes_the_invoice_it_pays(self, _mail):
        res = self.pay('1180.00', [{'invoice_id': str(self.ti.pk), 'amount': '1180.00'}])
        self.assertEqual(res.status_code, 201, res.data)
        row = DocumentSettlement.objects.get()
        self.assertEqual((str(row.payer_id), row.invoice_id, row.amount), (res.data['id'], self.ti.pk, Decimal('1180.00')))
        self.assertEqual(row.created_by, self.manager)
        self.assertEqual([line['document_number'] for line in res.data['settles']], [self.ti.document_number])

        detail = self.client.get(DETAIL.format(self.ti.pk)).data
        self.assertEqual((detail['balance']['open'], detail['balance']['status']), ('0.00', 'paid'))
        self.assertEqual([line['document_number'] for line in detail['settled_by']], [res.data['document_number']])

    def test_a_part_payment_leaves_the_rest_open_and_a_second_receipt_closes_it(self, _mail):
        first = self.pay('500.00', [{'invoice_id': str(self.ti.pk), 'amount': '500.00'}])
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(settlement.balance_of(self.ti).status, 'partial')
        self.assertEqual(settlement.balance_of(self.ti).open, Decimal('680.00'))
        second = self.pay('680.00', [{'invoice_id': str(self.ti.pk), 'amount': '680.00'}])
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(settlement.balance_of(self.ti).open, Decimal('0.00'))

    def test_more_than_is_open_is_refused_and_no_receipt_number_is_used(self, _mail):
        self.pay('1000.00', [{'invoice_id': str(self.ti.pk), 'amount': '1000.00'}])
        counter = self.counter('RC')
        res = self.pay('200.00', [{'invoice_id': str(self.ti.pk), 'amount': '200.00'}])
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('נותרו לתשלום 180.00', res.data['error'])
        self.assertEqual(self.counter('RC'), counter)
        self.assertEqual(FormalDocument.objects.filter(document_type='receipt').count(), 1)

    def test_paying_the_same_invoice_again_after_it_is_closed_is_refused(self, _mail):
        payload = [{'invoice_id': str(self.ti.pk), 'amount': '1180.00'}]
        self.assertEqual(self.pay('1180.00', payload).status_code, 201)
        retry = self.pay('1180.00', payload)
        self.assertEqual(retry.status_code, 400, retry.data)
        self.assertEqual(DocumentSettlement.objects.count(), 1)

    def test_invoices_worth_more_than_the_receipt_are_refused(self, _mail):
        other = self.invoice(f'TI-{self.year}-000901', total='500.00')
        res = self.pay('1000.00', [
            {'invoice_id': str(self.ti.pk), 'amount': '800.00'},
            {'invoice_id': str(other.pk), 'amount': '300.00'},
        ])
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('גדול מסכום', res.data['error'])
        self.assertFalse(DocumentSettlement.objects.exists())

    def test_one_receipt_can_pay_two_invoices(self, _mail):
        other = self.invoice(f'TI-{self.year}-000901', total='500.00')
        res = self.pay('1680.00', [
            {'invoice_id': str(self.ti.pk), 'amount': '1180.00'},
            {'invoice_id': str(other.pk), 'amount': '500.00'},
        ])
        self.assertEqual(res.status_code, 201, res.data)
        found = settlement.balances([self.ti, other])
        self.assertEqual([found[self.ti.pk].open, found[other.pk].open], [Decimal('0.00'), Decimal('0.00')])

    def test_the_same_invoice_twice_in_one_receipt_is_refused(self, _mail):
        row = {'invoice_id': str(self.ti.pk), 'amount': '100.00'}
        res = self.pay('200.00', [row, row])
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('פעמיים', res.data['error'])

    def test_another_customers_invoice_is_refused(self, _mail):
        theirs = self.invoice(f'TI-{self.year}-000902', child=self.other_kid())
        res = self.pay('100.00', [{'invoice_id': str(theirs.pk), 'amount': '100.00'}])
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('ללקוח אחר', res.data['error'])

    def test_a_receipt_does_not_close_a_transaction_invoice(self, _mail):
        tx = self.invoice(f'TX-{self.year}-000900', kind='transaction_invoice')
        res = self.pay('100.00', [{'invoice_id': str(tx.pk), 'amount': '100.00'}])
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('חשבונית מס/קבלה', res.data['error'])

    def test_an_invoice_receipt_closes_a_transaction_invoice_but_not_a_tax_invoice(self, _mail):
        tx = self.invoice(f'TX-{self.year}-000900', kind='transaction_invoice', total='118.00')
        ok = self.client.post(CREATE, irm_payload(self.kid, '100.00', day=self.today, settlements=[
            {'invoice_id': str(tx.pk), 'amount': '118.00'},
        ]), format='json')
        self.assertEqual(ok.status_code, 201, ok.data)
        self.assertEqual(settlement.balance_of(tx).status, 'paid')

        refused = self.client.post(CREATE, irm_payload(self.kid, '100.00', day=self.today, settlements=[
            {'invoice_id': str(self.ti.pk), 'amount': '118.00'},
        ]), format='json')
        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertIn('פעם שנייה', refused.data['error'])

    def test_withholding_pays_its_part_of_the_invoice(self, _mail):
        res = self.pay('1150.00', [{'invoice_id': str(self.ti.pk), 'amount': '1180.00'}], withholding='30.00')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(settlement.balance_of(self.ti).status, 'paid')

    def test_settlements_on_a_tax_invoice_are_refused(self, _mail):
        payload = self.invoice_payload('tax_invoice', self.today)
        payload['settlements'] = [{'invoice_id': str(self.ti.pk), 'amount': '10.00'}]
        res = self.client.post(CREATE, payload, format='json')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('settlements', res.data)

    def test_the_older_forms_linked_invoice_pays_it(self, _mail):
        res = self.client.post(CREATE, receipt_payload(
            self.kid, '500.00', day=self.today, linked=self.ti.document_number,
        ), format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(DocumentSettlement.objects.get().amount, Decimal('500.00'))
        self.assertEqual(settlement.balance_of(self.ti).open, Decimal('680.00'))

    def test_a_linked_number_kogo_does_not_know_stays_text(self, _mail):
        res = self.client.post(CREATE, receipt_payload(self.kid, '500.00', day=self.today, linked='40413'),
                               format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertFalse(DocumentSettlement.objects.exists())
        self.assertEqual(FormalDocument.objects.get(pk=res.data['id']).linked_document_number, '40413')


@override_settings(TRANZILA_BILLING_TERMINAL='')
class OlderRecordsTests(SettlementFixture, APITestCase):
    """What production already holds pays its invoices without a backfill."""

    def test_a_receipt_that_named_the_invoice_before_settlements_pays_it(self):
        ti = self.invoice(f'TI-{self.year}-000900')
        self.invoice(f'RC-{self.year}-000900', kind='receipt', total='1180.00',
                     linked_document_number=ti.document_number)
        self.assertEqual(settlement.balance_of(ti).status, 'paid')

    def test_a_named_receipt_of_another_customer_pays_nothing(self):
        ti = self.invoice(f'TI-{self.year}-000900')
        self.invoice(f'RC-{self.year}-000900', kind='receipt', child=self.other_kid(),
                     linked_document_number=ti.document_number)
        self.assertEqual(settlement.balance_of(ti).status, 'open')

    def test_a_named_receipt_pays_no_more_than_the_invoice(self):
        ti = self.invoice(f'TI-{self.year}-000900', total='100.00')
        receipt = self.invoice(f'RC-{self.year}-000900', kind='receipt', total='500.00',
                               linked_document_number=ti.document_number)
        self.assertEqual(settlement.balance_of(ti).paid, Decimal('100.00'))
        self.assertEqual(settlement.applied_amounts([receipt])[receipt.pk], Decimal('100.00'))

    def test_a_credit_note_takes_its_part_off(self):
        ti = self.invoice(f'TI-{self.year}-000900')
        self.invoice(f'CR-{self.year}-000900', kind='credit_invoice', total='180.00', linked_document=ti,
                     linked_document_number=ti.document_number)
        self.invoice(f'CR-{self.year}-000901', kind='credit_invoice', total='1000.00',
                     linked_document_number=ti.document_number)
        balance = settlement.balance_of(ti)
        self.assertEqual((balance.credited, balance.open, balance.status), (Decimal('1180.00'), Decimal('0'), 'credited'))

    def test_the_two_directions_agree(self):
        ti = self.invoice(f'TI-{self.year}-000900')
        receipt = self.invoice(f'RC-{self.year}-000900', kind='receipt', total='600.00',
                               linked_document_number=ti.document_number)
        self.assertEqual(settlement.balance_of(ti).paid, settlement.applied_amounts([receipt])[receipt.pk])


@override_settings(TRANZILA_BILLING_TERMINAL='')
class VoidTests(SettlementFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_user('void-manager@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)
        self.ti = self.invoice(f'TI-{self.year}-000900')
        res = self.client.post(CREATE, receipt_payload(self.kid, '1180.00', day=self.today, settlements=[
            {'invoice_id': str(self.ti.pk), 'amount': '1180.00'},
        ]), format='json')
        self.row = DocumentSettlement.objects.get(payer_id=res.data['id'])

    def test_a_voided_settlement_stays_and_the_invoice_opens_again(self):
        res = self.client.post(VOID.format(self.row.pk), {'reason': 'נרשם בטעות'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['invoice_balance']['open'], '1180.00')
        self.row.refresh_from_db()
        self.assertIsNotNone(self.row.voided_at)
        self.assertEqual(self.row.voided_by, self.manager)
        self.assertEqual(DocumentSettlement.objects.count(), 1)
        detail = self.client.get(DETAIL.format(self.ti.pk)).data
        self.assertIsNotNone(detail['settled_by'][0]['voided_at'])

    def test_voiding_twice_is_refused(self):
        self.client.post(VOID.format(self.row.pk), {'reason': 'טעות'}, format='json')
        res = self.client.post(VOID.format(self.row.pk), {'reason': 'טעות'}, format='json')
        self.assertEqual(res.status_code, 409, res.data)

    def test_a_reason_is_required(self):
        res = self.client.post(VOID.format(self.row.pk), {}, format='json')
        self.assertEqual(res.status_code, 400, res.data)
        self.row.refresh_from_db()
        self.assertIsNone(self.row.voided_at)

    def test_a_partner_cannot_void(self):
        self.client.force_authenticate(make_user('void-partner@test', UserProfile.ROLE_PARTNER))
        res = self.client.post(VOID.format(self.row.pk), {'reason': 'טעות'}, format='json')
        self.assertEqual(res.status_code, 403, res.data)

    def test_after_a_void_the_invoice_can_be_paid_again(self):
        self.client.post(VOID.format(self.row.pk), {'reason': 'טעות'}, format='json')
        res = self.client.post(CREATE, receipt_payload(self.kid, '1180.00', day=self.today, settlements=[
            {'invoice_id': str(self.ti.pk), 'amount': '1180.00'},
        ]), format='json')
        self.assertEqual(res.status_code, 201, res.data)


@override_settings(TRANZILA_BILLING_TERMINAL='')
class OpenInvoicesTests(SettlementFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(make_user('open-manager@test', UserProfile.ROLE_MANAGER))

    def test_the_picker_lists_what_is_still_open_oldest_first(self):
        paid = self.invoice(f'TI-{self.year}-000900', total='100.00')
        self.invoice(f'RC-{self.year}-000900', kind='receipt', total='100.00', linked_document_number=paid.document_number)
        newer = self.invoice(f'TI-{self.year}-000902', total='300.00')
        older = self.invoice(f'TI-{self.year}-000901', total='200.00', day=date(self.year, 1, 1))
        self.invoice(f'TX-{self.year}-000900', kind='transaction_invoice')
        self.invoice(f'TI-{self.year}-000903', child=self.other_kid())

        res = self.client.get(OPEN, {'child_id': str(self.kid.id)})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual([row['document_number'] for row in res.data['results']],
                         [older.document_number, newer.document_number])
        self.assertEqual(res.data['open_total'], '500.00')

        tx = self.client.get(OPEN, {'child_id': str(self.kid.id), 'payer_type': 'combined'})
        self.assertEqual([row['document_number'] for row in tx.data['results']], [f'TX-{self.year}-000900'])

    def test_a_customer_is_required(self):
        self.assertEqual(self.client.get(OPEN).status_code, 400)
        self.assertEqual(self.client.get(OPEN, {'child_id': 'nope'}).status_code, 400)

    def test_a_business_customers_invoices(self):
        customer = make_customer('סטודיו', 'אור')
        doc = FormalDocument.objects.create(
            document_number=f'TI-{self.year}-000950', document_type='tax_invoice', client_type='business',
            business_customer=customer, document_date=self.today, total_amount=Decimal('590.00'),
        )
        res = self.client.get(OPEN, {'business_customer_id': str(customer.id)})
        self.assertEqual([row['id'] for row in res.data['results']], [str(doc.pk)])


@override_settings(TRANZILA_BILLING_TERMINAL='')
class PartnerScopeTests(SettlementFixture, APITestCase):
    """A partner settles and sees only their own branches' invoices (WS-7's rule)."""

    def setUp(self):
        super().setUp()
        self.mine = self.kid.family.branch
        self.elsewhere = make_branch('רחוק')
        self.client.force_authenticate(make_scoped_user('settle-partner@test', UserProfile.ROLE_PARTNER, [self.mine]))

    def test_another_branchs_invoice_is_neither_listed_nor_payable(self):
        ours = self.invoice(f'TI-{self.year}-000900')
        theirs = self.invoice(f'TI-{self.year}-000901', branch=self.elsewhere)
        res = self.client.get(OPEN, {'child_id': str(self.kid.id)})
        self.assertEqual([row['id'] for row in res.data['results']], [str(ours.pk)])

        refused = self.client.post(CREATE, receipt_payload(self.kid, '100.00', day=self.today, settlements=[
            {'invoice_id': str(theirs.pk), 'amount': '100.00'},
        ]), format='json')
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertFalse(FormalDocument.objects.filter(document_type='receipt').exists())

        ok = self.client.post(CREATE, receipt_payload(self.kid, '100.00', day=self.today, settlements=[
            {'invoice_id': str(ours.pk), 'amount': '100.00'},
        ]), format='json')
        self.assertEqual(ok.status_code, 201, ok.data)


@override_settings(TRANZILA_BILLING_TERMINAL='')
class PagesTests(SettlementFixture, APITestCase):
    """The invoice's page and the receipt's page say what they said when issued — a copy is the original."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(make_user('pages-manager@test', UserProfile.ROLE_MANAGER))

    @staticmethod
    def fields(doc):
        return {field.label: field.value for field in build_document_layout(doc).payment_fields}

    def test_the_receipt_names_the_invoice_it_paid(self):
        ti = self.invoice(f'TI-{self.year}-000900', issued_at=None)
        res = self.client.post(CREATE, receipt_payload(self.kid, '1180.00', day=self.today, settlements=[
            {'invoice_id': str(ti.pk), 'amount': '1180.00'},
        ]), format='json')
        receipt = FormalDocument.objects.get(pk=res.data['id'])
        self.assertEqual(self.fields(receipt)['עבור חשבונית'], f'{ti.document_number} · ₪1180.00')

    def test_an_invoice_paid_later_still_prints_as_it_was_issued(self):
        ti = self.invoice(f'TI-{self.year}-000900')
        self.client.post(CREATE, receipt_payload(self.kid, '1180.00', day=self.today, settlements=[
            {'invoice_id': str(ti.pk), 'amount': '1180.00'},
        ]), format='json')
        self.assertEqual(self.fields(ti)['סטטוס'], 'ממתין לתשלום')


@override_settings(TRANZILA_BILLING_TERMINAL='')
class CollectionsTests(SettlementFixture, APITestCase):
    """The documents ledger (collections tab) and the dashboard's invoicing figures."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(make_user('ledger-manager@test', UserProfile.ROLE_MANAGER))
        self.ti = self.invoice(f'TI-{self.year}-000900')
        res = self.client.post(CREATE, receipt_payload(self.kid, '1000.00', day=self.today, settlements=[
            {'invoice_id': str(self.ti.pk), 'amount': '1000.00'},
        ]), format='json')
        self.receipt_id = res.data['id']

    def rows(self):
        found = list_ledger_documents(self.today, self.today, local_only=True)['documents']
        return {row['id']: row for row in found}

    def test_the_invoice_row_shows_what_is_left(self):
        row = self.rows()[str(self.ti.pk)]
        self.assertEqual((row['amount_paid'], row['open_balance'], row['status']), (1000.0, 180.0, 'partially_paid'))
        receipt = self.rows()[self.receipt_id]
        self.assertEqual((receipt['applied_amount'], receipt['open_balance']), (1000.0, 0.0))

    def test_a_closed_invoice_is_not_a_debt(self):
        self.client.post(CREATE, receipt_payload(self.kid, '180.00', day=self.today, settlements=[
            {'invoice_id': str(self.ti.pk), 'amount': '180.00'},
        ]), format='json')
        row = self.rows()[str(self.ti.pk)]
        self.assertEqual((row['open_balance'], row['status']), (0.0, 'completed'))

    def test_the_dashboard_counts_the_receipts_money_once(self):
        res = self.client.get('/api/v1/core/dashboard/invoicing/', {
            'date_from': str(self.today), 'date_to': str(self.today),
        })
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data['invoiced'], res.data['collected'], res.data['open_balance']),
                         (1180.0, 1000.0, 180.0))


@override_settings(TRANZILA_BILLING_TERMINAL='')
class TwoReceiptsAtOnceTests(TransactionTestCase):
    """Two receipts for the whole of one invoice at the same moment: one pays it, the other is refused."""

    serialized_rollback = True

    def test_the_invoice_is_paid_once(self):
        fixture = SettlementFixture()
        fixture.setUp()
        ti = fixture.invoice(f'TI-{fixture.year}-000900')
        manager = make_user('race-settle@test', UserProfile.ROLE_MANAGER)
        start = threading.Barrier(2)
        results = []

        def pay():
            from django.db import transaction

            from apps.documents import service
            try:
                start.wait(timeout=10)
                with transaction.atomic():
                    doc = service.create_receipt(
                        receipt_payload(fixture.kid, '1180.00', day=fixture.today), issued_by=manager,
                    )
                    settlement.record_settlements(doc, [{'invoice_id': ti.pk, 'amount': '1180.00'}], user=manager)
                results.append('paid')
            except ValueError as exc:
                results.append(f'refused: {exc}')
            finally:
                connections.close_all()

        threads = [threading.Thread(target=pay) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(sorted(r.split(':')[0] for r in results), ['paid', 'refused'], results)
        self.assertEqual(DocumentSettlement.objects.filter(invoice=ti).count(), 1)
        self.assertEqual(FormalDocument.objects.filter(document_type='receipt').count(), 1)
        # The refused receipt rolled back with its number: the run has no gap.
        self.assertEqual(DocumentSeries.objects.get(series='RC', year=fixture.year).counter, 1)
