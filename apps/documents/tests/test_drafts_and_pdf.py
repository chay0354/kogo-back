"""Draft documents: numbering, approval, reporting, and the local PDF."""
import os
import re
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.customers.models import Child, Family
from apps.documents.models import DocumentCounter, FormalDocument

User = get_user_model()
CREATE = '/api/v1/documents/documents/create-document/'


def make_user(username, role):
    user = User.objects.create_user(username=username, password='pw-for-tests')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return User.objects.get(pk=user.pk)


class DraftDocumentTests(APITestCase):
    def setUp(self):
        city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='סניף', city=city)
        fam = Family.objects.create(name='משפחה', branch=self.branch)
        self.kid = Child.objects.create(
            family=fam, first_name='נועה', last_name='כהן',
            birth_date=date(2015, 5, 5), gender='female', status='active',
        )
        self.manager = make_user('m@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)

    def payload(self, doc_type, **extra):
        body = {
            'document_type': doc_type,
            'client_type': 'existing',
            'child_id': str(self.kid.id),
            'invoice_details': {
                'document_date': '2026-09-02',
                'line_items': [{'description': 'חוג ספטמבר', 'quantity': 1, 'price': '360.00'}],
            },
        }
        body.update(extra)
        return body

    def test_private_document_takes_the_familys_branch(self):
        res = self.client.post(CREATE, self.payload('tax_invoice'), format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(str(FormalDocument.objects.get(pk=res.data['id']).branch_id), str(self.branch.id))

    def test_a_document_can_carry_its_branch(self):
        res = self.client.post(CREATE, self.payload('tax_invoice', branch_id=str(self.branch.id)), format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(str(FormalDocument.objects.get(pk=res.data['id']).branch_id), str(self.branch.id))

    def test_draft_takes_no_fiscal_number(self):
        before = DocumentCounter.objects.filter(year=2026).first()
        before = before.counter if before else 0
        res = self.client.post(CREATE, self.payload('draft', draft_target_type='tax_invoice'), format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        doc = FormalDocument.objects.get(pk=res.data['id'])
        self.assertEqual(doc.document_type, 'draft')
        self.assertTrue(re.fullmatch(r'D-[0-9A-F]{8}', doc.document_number), doc.document_number)
        after = DocumentCounter.objects.filter(year=2026).first()
        self.assertEqual(after.counter if after else 0, before)
        self.assertFalse(doc.tranzila_issued)
        self.assertEqual(doc.total_amount, Decimal('424.80'))

    def test_a_missing_details_section_is_answered_before_it_reaches_the_service(self):
        body = self.payload('tax_invoice')
        body.pop('invoice_details')
        res = self.client.post(CREATE, body, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.data)
        self.assertIn('invoice_details', res.data)

    def test_a_combined_document_needs_only_the_invoice_section(self):
        res = self.client.post(CREATE, self.payload('combined'), format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)

    def test_a_transaction_invoice_carries_no_vat_however_it_was_issued(self):
        direct = self.client.post(CREATE, self.payload('transaction_invoice'), format='json')
        self.assertEqual(direct.status_code, status.HTTP_201_CREATED, direct.data)
        issued = FormalDocument.objects.get(pk=direct.data['id'])
        self.assertTrue(issued.vat_exempt)
        self.assertEqual(issued.vat_amount, Decimal('0.00'))
        self.assertEqual(issued.total_amount, Decimal('360.00'))

        drafted = self.client.post(
            CREATE, self.payload('draft', draft_target_type='transaction_invoice'), format='json',
        )
        approved = self.client.post(f"/api/v1/documents/documents/{drafted.data['id']}/finalize/")
        self.assertEqual(approved.status_code, status.HTTP_200_OK, approved.data)
        self.assertEqual(
            FormalDocument.objects.get(pk=drafted.data['id']).total_amount, issued.total_amount,
        )

    def test_finalize_gives_the_next_fiscal_number_and_the_target_type(self):
        real = self.client.post(CREATE, self.payload('tax_invoice'), format='json')
        self.assertEqual(real.status_code, 201, real.data)
        draft = self.client.post(CREATE, self.payload('draft', draft_target_type='tax_invoice'), format='json')
        res = self.client.post(f"/api/v1/documents/documents/{draft.data['id']}/finalize/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        doc = FormalDocument.objects.get(pk=draft.data['id'])
        self.assertEqual(doc.document_type, 'tax_invoice')
        real_seq = int(real.data['document_number'].split('-')[1])
        self.assertEqual(doc.document_number, f'2026-{real_seq + 1:04d}')

    def test_finalizing_a_real_document_is_refused(self):
        real = self.client.post(CREATE, self.payload('tax_invoice'), format='json')
        res = self.client.post(f"/api/v1/documents/documents/{real.data['id']}/finalize/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_draft_is_excluded_from_the_period_report(self):
        from apps.documents.period_report import build_report, parse_period
        self.client.post(CREATE, self.payload('draft', draft_target_type='tax_invoice'), format='json')
        self.client.post(CREATE, self.payload('tax_invoice'), format='json')
        start, end, label = parse_period({'month': '2026-09'})
        report = build_report(self.manager, start, end, label)
        self.assertEqual(report.totals.count, 1)
        self.assertNotIn('draft', report.type_totals)

    def test_draft_shows_in_the_ledger_as_a_draft(self):
        from apps.core.tranzila_ledger import list_ledger_documents
        res = self.client.post(CREATE, self.payload('draft', draft_target_type='tax_invoice'), format='json')
        rows = list_ledger_documents(date(2026, 9, 1), date(2026, 9, 30), local_only=True)
        rows = rows['documents'] if isinstance(rows, dict) else rows
        row = next(r for r in rows if r['id'] == res.data['id'])
        self.assertTrue(row['is_draft'])
        self.assertEqual(row['status'], 'draft')
        self.assertFalse(row['tranzila_issued'])
        self.assertEqual(row['open_balance'], 0.0)

    def test_every_type_renders_a_local_pdf(self):
        out_dir = os.environ.get('DOCUMENT_PDF_SAMPLES')
        cases = [
            ('tax_invoice', {}),
            ('transaction_invoice', {}),
            ('draft', {'draft_target_type': 'tax_invoice'}),
        ]
        for doc_type, extra in cases:
            res = self.client.post(CREATE, self.payload(doc_type, **extra), format='json')
            self.assertEqual(res.status_code, 201, (doc_type, res.data))
            pdf = self.client.get(f"/api/v1/documents/documents/{res.data['id']}/pdf/")
            self.assertEqual(pdf.status_code, 200, doc_type)
            self.assertTrue(pdf.content.startswith(b'%PDF'), doc_type)
            if out_dir:
                with open(os.path.join(out_dir, f'{doc_type}.pdf'), 'wb') as fh:
                    fh.write(pdf.content)
        receipt = self.client.post(CREATE, {
            'document_type': 'receipt', 'client_type': 'existing', 'child_id': str(self.kid.id),
            'receipt_details': {'document_date': '2026-09-02', 'payment_method': 'cash', 'amount': '424.80'},
        }, format='json')
        self.assertEqual(receipt.status_code, 201, receipt.data)
        pdf = self.client.get(f"/api/v1/documents/documents/{receipt.data['id']}/pdf/")
        self.assertEqual(pdf.status_code, 200)
        if out_dir:
            with open(os.path.join(out_dir, 'receipt.pdf'), 'wb') as fh:
                fh.write(pdf.content)
