"""
A lesson receipt (IR) downloads from the invoices page's documents tab — as a copy (audit M5, 30.9.2026).

The tab showed "ללא PDF" on every IR row: the ledger gave it no route to a
file. The row now carries its receipt's id, and the tab downloads through the
child card's own endpoint with ?copy=1: always "העתק", never the original, and
under the same partner branch rule as the list.
"""
from datetime import date
from unittest.mock import patch

from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.core.tranzila_ledger import list_ledger_documents
from apps.customers.financial_models import InvoiceActivityLog
from apps.customers.subscription_invoice_pdf import ORIGINAL_PRODUCED
from apps.documents.issuer import COPY_MARK, ORIGINAL_MARK
from apps.documents.tests.test_register import RegisterFixture, make_user

AUGUST = (date(2026, 8, 1), date(2026, 8, 31))


def marks_only():
    """The PDF reduced to its "מקור" / "העתק" mark."""
    return patch('apps.customers.subscription_invoice_pdf.render_invoice_pdf',
                 side_effect=lambda layout: layout.copy_mark.encode())


class LessonReceiptCopyTests(RegisterFixture, APITestCase):
    def url(self, invoice, copy=True) -> str:
        return f'/api/v1/customers/invoices/{invoice.id}/pdf/' + ('?copy=1' if copy else '')

    def test_the_ledger_row_carries_the_receipts_id(self):
        invoice = self.lesson_receipt('IR-2026-000070')
        rows = list_ledger_documents(*AUGUST, local_only=True)['documents']
        row = next(item for item in rows if item['document_number'] == invoice.invoice_number)
        self.assertEqual(row['document_type_code'], 'IR')
        self.assertEqual(row['lesson_invoice_id'], str(invoice.id))

    def test_the_documents_tab_download_is_always_a_copy_and_leaves_the_original_where_it_was(self):
        invoice = self.lesson_receipt('IR-2026-000071')  # never mailed: the original has not left
        self.client.force_authenticate(self.manager)
        with marks_only():
            copies = [self.client.get(self.url(invoice)) for _ in range(2)]
            for response in copies:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Content-Type'], 'application/pdf')
                self.assertIn(invoice.invoice_number, response['Content-Disposition'])
            self.assertEqual([response.content.decode() for response in copies], [COPY_MARK, COPY_MARK])
            # A copy is not the original's one print: the child card's download still is.
            self.assertFalse(InvoiceActivityLog.objects.filter(invoice=invoice, action=ORIGINAL_PRODUCED).exists())
            self.assertEqual(self.client.get(self.url(invoice, copy=False)).content.decode(), ORIGINAL_MARK)

    def test_a_partner_reaches_only_the_receipts_of_their_own_branches(self):
        north = self.lesson_receipt('IR-2026-000072', branch=self.north)
        south = self.lesson_receipt('IR-2026-000073', branch=self.south)
        partner = make_user('partner-ir-copy@test', UserProfile.ROLE_PARTNER)
        partner.profile.assigned_branches.set([self.north])
        self.client.force_authenticate(partner)
        with marks_only():
            self.assertEqual(self.client.get(self.url(north)).status_code, 200)
            self.assertEqual(self.client.get(self.url(south)).status_code, 404)
        # The list the partner sees holds the same one only.
        numbers = {row['document_number'] for row in
                   list_ledger_documents(*AUGUST, local_only=True, branch_ids=[self.north.id])['documents']}
        self.assertIn(north.invoice_number, numbers)
        self.assertNotIn(south.invoice_number, numbers)

    def test_nobody_else_downloads_it(self):
        invoice = self.lesson_receipt('IR-2026-000074')
        self.assertEqual(self.client.get(self.url(invoice)).status_code, 401)
        worker = make_user('worker-ir-copy@test', UserProfile.ROLE_WORKER)
        self.client.force_authenticate(worker)
        self.assertEqual(self.client.get(self.url(invoice)).status_code, 403)
