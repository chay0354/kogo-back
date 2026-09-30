"""
The business customer's card (…/business-customers/{id}/summary/) and the
delivery status the child's card now shows beside each document.

What is pinned here: the card lists every document issued to the customer —
rent receipts and credit notes included — newest first with how each signed
original reached them, drafts apart; the previous software's history for
managers only; the tenancies they hold; and a partner reaches nothing outside
their branches.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.core.payment_service import PaymentService
from apps.core.tests.test_fixtures import TestDataFactory
from apps.core.tranzila_ledger import list_ledger_documents
from apps.customers.models import Payment
from apps.documents.models import FormalDocument, SignedOriginal
from apps.legacy_import.models import LegacyDocument
from apps.rentals.tests.factories import make_branch, make_customer, make_rental, make_tenancy, make_user
from apps.store.models import StoreInvoice

URL = '/api/v1/customers/business-customers/{}/summary/'


def formal(customer, number, document_type, *, day, total='100.00', **fields):
    return FormalDocument.objects.create(
        document_number=number,
        document_type=document_type,
        client_type='business',
        business_customer=customer,
        document_date=day,
        total_amount=Decimal(total),
        **fields,
    )


def original(doc_or_number, *, kind=SignedOriginal.KIND_FORMAL, source_id='', signed=False, **fields):
    """A stored original row as the signing service leaves it. Signed rows carry bytes, as the table demands."""
    number = getattr(doc_or_number, 'document_number', doc_or_number)
    if not source_id and hasattr(doc_or_number, 'pk'):
        source_id = str(doc_or_number.pk)
    values = {'number': number, 'kind': kind, 'source_id': source_id, **fields}
    if signed:
        values.update(pdf=b'%PDF-signed', sha256='a' * 64, size=11, signed_at=timezone.now())
    return SignedOriginal.objects.create(**values)


class BusinessCustomerSummaryTests(APITestCase):
    def setUp(self):
        self.branch = make_branch('פלורנטין')
        self.other_branch = make_branch('רמת אביב')
        self.manager = make_user('manager-card@test', UserProfile.ROLE_MANAGER)
        self.partner = make_user('partner-card@test', UserProfile.ROLE_PARTNER, [self.branch])
        self.customer = make_customer(
            'סטודיו', 'אור', company_number='512345678', email='or@example.com', branch=self.branch,
        )
        self.client.force_authenticate(self.manager)

    def get(self, customer=None):
        return self.client.get(URL.format((customer or self.customer).id))

    def test_the_card_lists_every_issued_document_newest_first_with_drafts_apart(self):
        invoice = formal(self.customer, 'TI-2026-000001', 'tax_invoice', day=date(2026, 9, 1), total='590.00')
        receipt = formal(self.customer, 'RC-2026-000001', 'receipt', day=date(2026, 9, 5), total='590.00')
        rent = formal(self.customer, 'RT-2026-000001', 'combined', day=date(2026, 9, 10), total='1456.78')
        credit = formal(
            self.customer, 'CR-2026-000001', 'credit_invoice', day=date(2026, 9, 12), total='100.00',
            credit_reason='הנחה שסוכמה', linked_document_number='TI-2026-000001',
        )
        draft = formal(
            self.customer, 'DRAFT-1', 'draft', day=date(2026, 9, 20), total='50.00', draft_target_type='tax_invoice',
        )
        formal(make_customer('אחר', 'לגמרי'), 'TI-2026-000002', 'tax_invoice', day=date(2026, 9, 15))

        res = self.get()

        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        numbers = [row['document_number'] for row in res.data['documents']]
        self.assertEqual(numbers, [credit.document_number, rent.document_number,
                                   receipt.document_number, invoice.document_number])
        self.assertEqual([row['id'] for row in res.data['drafts']], [str(draft.id)])
        self.assertEqual(res.data['drafts'][0]['target_type_label'], 'חשבונית מס')

        credit_row, rent_row = res.data['documents'][0], res.data['documents'][1]
        self.assertTrue(credit_row['is_credit'])
        self.assertEqual(credit_row['description'], 'הנחה שסוכמה')
        self.assertEqual(credit_row['linked_document_number'], 'TI-2026-000001')
        self.assertEqual(credit_row['document_type_label'], 'חשבונית מס זיכוי')
        self.assertTrue(rent_row['is_rental'])
        self.assertFalse(credit_row['is_rental'])
        self.assertEqual(rent_row['date'], '2026-09-10')
        self.assertEqual(rent_row['total'], '1456.78')
        self.assertEqual(rent_row['download_url'], f'/documents/documents/{rent.id}/pdf/')

        totals = res.data['totals']
        self.assertEqual(totals['documents_count'], 4)
        self.assertEqual(totals['drafts_count'], 1)
        # TI + the RT's חשבונית מס/קבלה; received: RC + RT; the draft counts nowhere.
        self.assertEqual(totals['invoiced'], '2046.78')
        self.assertEqual(totals['received'], '2046.78')
        self.assertEqual(totals['credited'], '100.00')
        self.assertEqual(totals['net_invoiced'], '1946.78')
        self.assertEqual(
            [(line['document_type'], line['count']) for line in totals['by_type']],
            [('tax_invoice', 1), ('receipt', 1), ('combined', 1), ('credit_invoice', 1)],
        )
        # The balance (settlement.py): the TI less the credit note naming it.
        # The receipt names no invoice, so it pays none of it.
        balance = res.data['balance']
        self.assertEqual((balance['open_total'], balance['open_count']), ('490.00', 1))
        self.assertEqual(balance['credited_total'], '100.00')
        self.assertEqual(
            [(row['document_number'], row['open'], row['status']) for row in balance['open_invoices']],
            [('TI-2026-000001', '490.00', 'partial')],
        )

    def test_identity_and_consent_are_the_customers_own(self):
        moment = timezone.now() - timedelta(days=3)
        self.customer.computerized_docs_consent_at = moment
        self.customer.computerized_docs_consent_source = 'crm'
        self.customer.save()

        res = self.get()

        self.assertEqual(res.data['customer']['id'], str(self.customer.id))
        self.assertEqual(res.data['customer']['company_number'], '512345678')
        self.assertEqual(res.data['customer']['branch_name'], 'פלורנטין')
        consent = res.data['consent']
        self.assertTrue(consent['accepts_computerized_documents'])
        self.assertEqual(consent['computerized_docs_consent_source'], 'crm')
        # On Israel's clock with its offset, as the customer's own record writes it.
        self.assertEqual(consent['computerized_docs_consent_at'], timezone.localtime(moment).isoformat())
        self.assertIsNone(consent['computerized_docs_consent_revoked_at'])

    def test_each_document_says_how_its_original_was_delivered_or_null(self):
        mailed = formal(self.customer, 'TI-2026-000010', 'tax_invoice', day=date(2026, 9, 1))
        paper = formal(self.customer, 'RC-2026-000010', 'receipt', day=date(2026, 9, 2))
        held = formal(self.customer, 'RC-2026-000011', 'receipt', day=date(2026, 9, 3))
        archived = formal(self.customer, 'TI-2026-000011', 'tax_invoice', day=date(2026, 9, 4))
        by_number = formal(self.customer, 'TI-2026-000012', 'tax_invoice', day=date(2026, 9, 5))
        untouched = formal(self.customer, 'TI-2026-000013', 'tax_invoice', day=date(2026, 9, 6))

        sent_at = timezone.now() - timedelta(hours=2)
        printed_at = timezone.now() - timedelta(hours=1)
        original(mailed, signed=True, delivery=SignedOriginal.DELIVERY_EMAIL, delivery_reason='נשלח', sent_at=sent_at)
        original(paper, signed=True, delivery=SignedOriginal.DELIVERY_PAPER,
                 delivery_reason='שולם במזומן', paper_original_printed_at=printed_at)
        original(held, delivery=SignedOriginal.DELIVERY_HELD, delivery_reason='ממתין לחתימה')
        original(archived, signed=True, purpose=SignedOriginal.PURPOSE_ARCHIVE,
                 delivery=SignedOriginal.DELIVERY_NONE, delivery_reason='העתק לארכיון')
        # Keyed to another row id but carrying this document's number: found by the number.
        original(by_number, source_id='not-this-id', delivery=SignedOriginal.DELIVERY_PAPER)

        rows = {row['document_number']: row['delivery_status'] for row in self.get().data['documents']}

        self.assertEqual(rows[mailed.document_number]['delivery'], 'email')
        self.assertEqual(rows[mailed.document_number]['sent_at'], timezone.localtime(sent_at).isoformat())
        self.assertEqual(rows[mailed.document_number]['purpose'], 'original')
        self.assertIsNotNone(rows[mailed.document_number]['signed_at'])
        self.assertEqual(rows[paper.document_number]['delivery'], 'paper')
        self.assertEqual(
            rows[paper.document_number]['paper_original_printed_at'], timezone.localtime(printed_at).isoformat(),
        )
        self.assertEqual(rows[paper.document_number]['delivery_reason'], 'שולם במזומן')
        self.assertEqual(rows[held.document_number]['delivery'], 'held')
        self.assertEqual(rows[held.document_number]['delivery_reason'], 'ממתין לחתימה')
        self.assertIsNone(rows[held.document_number]['signed_at'])
        self.assertEqual(rows[archived.document_number]['purpose'], 'archive')
        self.assertEqual(rows[by_number.document_number]['delivery'], 'paper')
        self.assertIsNone(rows[untouched.document_number])
        self.assertEqual(
            set(rows[mailed.document_number]),
            {'delivery', 'delivery_reason', 'purpose', 'sent_at', 'paper_original_printed_at', 'signed_at'},
        )

    def test_the_previous_softwares_history_is_on_the_card_for_a_manager_only(self):
        LegacyDocument.objects.create(
            original_type='חשבונית מס', doc_type='tax_invoice', number=4411, document_date=date(2025, 12, 1),
            invoice_total=Decimal('300.00'), business_customer=self.customer,
        )
        LegacyDocument.objects.create(
            original_type='קבלה', doc_type='receipt', number=77, document_date=date(2026, 1, 2),
            receipt_total=Decimal('300.00'), business_customer=self.customer,
        )
        LegacyDocument.objects.create(
            original_type='קבלה', doc_type='receipt', number=78, document_date=date(2026, 1, 3),
            business_customer=make_customer('לקוח', 'אחר'),
        )

        legacy = self.get().data['legacy']

        self.assertEqual(legacy['count'], 2)
        self.assertFalse(legacy['truncated'])
        self.assertEqual([row['number'] for row in legacy['results']], [77, 4411])
        self.assertEqual({row['source'] for row in legacy['results']}, {'legacy'})

        self.client.force_authenticate(self.partner)
        res = self.get()
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIsNone(res.data['legacy'])

    def test_a_tenant_card_shows_its_tenancies_and_slots(self):
        tenancy = make_tenancy(self.branch, tenant=self.customer, status='active')
        slot = make_rental(self.branch, renter_name='סטודיו אור')
        slot.tenancy = tenancy
        slot.save(update_fields=['tenancy'])
        make_tenancy(self.branch, tenant=self.customer, status='ended')

        res = self.get()

        self.assertTrue(res.data['is_tenant'])
        self.assertEqual([row['status'] for row in res.data['tenancies']], ['active', 'ended'])
        active = res.data['tenancies'][0]
        self.assertEqual(active['monthly_amount'], '1234.56')
        self.assertEqual(active['billing_day'], 10)
        self.assertEqual(active['branch_name'], 'פלורנטין')
        self.assertEqual([row['id'] for row in active['slots']], [str(slot.id)])

    def test_a_customer_who_rents_nothing_is_not_a_tenant(self):
        res = self.get()
        self.assertFalse(res.data['is_tenant'])
        self.assertEqual(res.data['tenancies'], [])
        self.assertEqual(res.data['documents'], [])
        self.assertEqual(res.data['totals']['documents_count'], 0)

    def test_a_partner_gets_404_for_another_branchs_customer(self):
        theirs = make_customer('רון', 'בר', branch=self.other_branch)
        self.client.force_authenticate(self.partner)
        self.assertEqual(self.get(theirs).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.get().status_code, status.HTTP_200_OK)

    def test_a_partner_with_no_branch_reaches_no_card(self):
        self.client.force_authenticate(make_user('partner-card-none@test', UserProfile.ROLE_PARTNER))
        self.assertEqual(self.get().status_code, status.HTTP_404_NOT_FOUND)

    def test_a_worker_is_refused(self):
        self.client.force_authenticate(make_user('worker-card@test', UserProfile.ROLE_WORKER))
        self.assertEqual(self.get().status_code, status.HTTP_403_FORBIDDEN)

    def test_on_a_branchless_card_a_partner_sees_only_their_branches_documents(self):
        shared = make_customer('עיריית', 'העיר')
        mine = formal(shared, 'TI-2026-000020', 'tax_invoice', day=date(2026, 9, 1), branch=self.branch)
        formal(shared, 'TI-2026-000021', 'tax_invoice', day=date(2026, 9, 2), branch=self.other_branch)
        formal(shared, 'TI-2026-000022', 'tax_invoice', day=date(2026, 9, 3))
        other_tenancy = make_tenancy(self.other_branch, tenant=shared)

        self.client.force_authenticate(self.partner)
        res = self.get(shared)

        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual([row['id'] for row in res.data['documents']], [str(mine.id)])
        self.assertEqual(res.data['totals']['documents_count'], 1)
        self.assertEqual(res.data['tenancies'], [])

        self.client.force_authenticate(self.manager)
        res = self.get(shared)
        self.assertEqual(len(res.data['documents']), 3)
        self.assertEqual([row['id'] for row in res.data['tenancies']], [str(other_tenancy.id)])

    def test_on_their_own_branchs_card_a_partner_sees_documents_with_no_branch(self):
        doc = formal(self.customer, 'TI-2026-000030', 'tax_invoice', day=date(2026, 9, 1))
        formal(self.customer, 'TI-2026-000031', 'tax_invoice', day=date(2026, 9, 2), branch=self.other_branch)
        self.client.force_authenticate(self.partner)
        self.assertEqual([row['id'] for row in self.get().data['documents']], [str(doc.id)])

    def test_the_download_route_is_the_office_copy_that_already_exists(self):
        doc = formal(self.customer, 'TI-2026-000040', 'tax_invoice', day=date(2026, 9, 1))
        row = self.get().data['documents'][0]
        res = self.client.get(f"/api/v1{row['download_url']}")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res['Content-Type'], 'application/pdf')
        self.assertIn(doc.document_number, res['Content-Disposition'])

    def test_the_card_never_reads_the_stored_bytes(self):
        doc = formal(self.customer, 'TI-2026-000050', 'tax_invoice', day=date(2026, 9, 1))
        original(doc, signed=True, delivery=SignedOriginal.DELIVERY_EMAIL)
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.get().status_code, status.HTTP_200_OK)
        signed_reads = [q['sql'] for q in queries.captured_queries if 'signed_originals' in q['sql']]
        self.assertEqual(len(signed_reads), 1)
        self.assertNotIn('"pdf"', signed_reads[0])

    def test_the_ledger_row_names_its_business_customer(self):
        doc = formal(self.customer, 'TI-2026-000060', 'tax_invoice', day=timezone.localdate())
        rows = list_ledger_documents(local_only=True)['documents']
        row = next(item for item in rows if item['id'] == str(doc.id))
        self.assertEqual(row['business_customer_id'], str(self.customer.id))


class ChildDocumentsDeliveryTests(APITestCase):
    """The child's card: each document now says how its original went — and nothing else changed."""

    def setUp(self):
        manager = make_user('manager-child-delivery@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(manager)
        course = TestDataFactory.create_course(name='ג׳ודו')
        instructor = TestDataFactory.create_instructor(branch=course.branch)
        self.lesson = TestDataFactory.create_lesson(course=course, instructor=instructor)
        self.family = TestDataFactory.create_family(email='parent@example.com')
        self.child = TestDataFactory.create_child(family=self.family)
        self.branch = course.branch

    def rows(self):
        res = self.client.get(f'/api/v1/customers/children/{self.child.id}/documents/')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        return {row['document_number']: row for row in res.data['documents']}

    def test_every_kind_of_row_carries_its_delivery_status_or_null(self):
        payment = Payment.objects.create(
            child=self.child, family=self.family, lesson=self.lesson, branch=self.branch,
            payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('236.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('236.00'),
            payment_date=timezone.now(),
        )
        receipt = PaymentService()._create_invoice_from_payment(payment, None, send_email=False)
        sale = StoreInvoice.objects.create(child=self.child, total_amount=Decimal('49.00'), payment_method='cash',
                                           payment_status='completed')
        manual = FormalDocument.objects.create(
            document_number='TI-2026-000070', document_type='tax_invoice', client_type='existing',
            child=self.child, document_date=date(2026, 9, 1), total_amount=Decimal('80.00'),
        )
        # Signing is off in tests, so issuing stored nothing: each row says what the test stores.
        self.assertFalse(SignedOriginal.objects.exists())
        sent_at = timezone.now()
        original(receipt.invoice_number, kind=SignedOriginal.KIND_IR, source_id=str(receipt.id),
                 signed=True, delivery=SignedOriginal.DELIVERY_EMAIL, sent_at=sent_at)
        original(manual, delivery=SignedOriginal.DELIVERY_HELD, delivery_reason='ממתין לחתימה')

        rows = self.rows()

        self.assertEqual(rows[receipt.invoice_number]['delivery_status']['delivery'], 'email')
        self.assertEqual(
            rows[receipt.invoice_number]['delivery_status']['sent_at'], timezone.localtime(sent_at).isoformat(),
        )
        self.assertEqual(rows[manual.document_number]['delivery_status']['delivery'], 'held')
        self.assertEqual(rows[manual.document_number]['delivery_status']['delivery_reason'], 'ממתין לחתימה')
        self.assertIsNone(rows[sale.invoice_number]['delivery_status'])
        # The rest of the row is as it was.
        self.assertEqual(rows[receipt.invoice_number]['kind'], 'receipt')
        self.assertEqual(rows[receipt.invoice_number]['download_url'], f'/customers/invoices/{receipt.id}/pdf/')
        self.assertEqual(rows[manual.document_number]['download_url'], f'/documents/documents/{manual.id}/pdf/')

    def test_a_child_with_no_stored_originals_reads_null_on_every_row(self):
        FormalDocument.objects.create(
            document_number='TI-2026-000071', document_type='tax_invoice', client_type='existing',
            child=self.child, document_date=date(2026, 9, 1), total_amount=Decimal('80.00'),
        )
        rows = self.rows()
        self.assertEqual(list(rows), ['TI-2026-000071'])
        self.assertIsNone(rows['TI-2026-000071']['delivery_status'])
