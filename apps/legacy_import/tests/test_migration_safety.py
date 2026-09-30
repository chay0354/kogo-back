"""
legacy_import 0002 against the code that is already running: a Vercel build of
this branch migrates production while production's code — which writes none of
the new columns — keeps serving. Rows written the old way must still go in,
get 'tazman', and be refused only for what the old key refused.
"""
import uuid

from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from apps.legacy_import.models import LegacyDocument, LegacyImport

OLD_DOCUMENT_INSERT = """
INSERT INTO legacy_documents (id, source_import_id, original_type, doc_type, number, document_date,
    invoice_total, receipt_total, credit_total, withholding_amount, total_before_withholding, original_status,
    payment_type, card_last_four, location, details, remark, customer_key, customer_name, customer_email,
    customer_phone, created_at, updated_at)
VALUES (%s, NULL, 'חשבונית מס', 'tax_invoice', %s, '2025-01-01', 100, 0, 0, 0, 100, '', '', '', '', '', '',
    '', 'לקוח', '', '', now(), now())
"""
OLD_IMPORT_INSERT = """
INSERT INTO legacy_imports (id, file_name, sha256, row_count, status, rows, summary, mapping,
    include_subscription_parents, result, uploaded_at)
VALUES (%s, 'old.xls', %s, 0, 'preview', '[]', '{}', '{}', false, '{}', now())
"""


def old_insert_document(number):
    with connection.cursor() as cursor:
        cursor.execute(OLD_DOCUMENT_INSERT, [uuid.uuid4(), number])


class OldCodeTests(TestCase):
    def test_a_row_written_without_the_new_columns_gets_their_defaults(self):
        old_insert_document(40001)
        with connection.cursor() as cursor:
            cursor.execute(OLD_IMPORT_INSERT, [uuid.uuid4(), 'c' * 64])

        doc = LegacyDocument.objects.get(number=40001)
        self.assertEqual(doc.source_system, 'tazman')
        self.assertEqual((doc.original_number, doc.allocation_number, doc.linked_document), ('', '', ''))
        self.assertEqual((doc.pdf_sha256, doc.pdf_object, doc.pdf_file_name), ('', '', ''))
        self.assertIsNone(doc.amount_before_vat)
        self.assertIsNone(doc.pdf_size)
        self.assertEqual(LegacyImport.objects.get().source_system, 'tazman')

    def test_the_old_code_is_refused_only_what_the_old_key_refused(self):
        old_insert_document(40002)
        with self.assertRaises(IntegrityError), transaction.atomic():
            old_insert_document(40002)
        # Another type with the same number was always allowed.
        with connection.cursor() as cursor:
            cursor.execute(OLD_DOCUMENT_INSERT.replace("'tax_invoice'", "'receipt'"), [uuid.uuid4(), 40002])
        self.assertEqual(LegacyDocument.objects.filter(number=40002).count(), 2)

    def test_two_softwares_may_share_a_type_and_number(self):
        old_insert_document(40003)
        LegacyDocument.objects.create(
            source_system='greeninvoice', original_type='חשבונית מס', doc_type='tax_invoice', number=40003,
            document_date='2025-02-01',
        )
        self.assertEqual(
            sorted(LegacyDocument.objects.filter(number=40003).values_list('source_system', flat=True)),
            ['greeninvoice', 'tazman'],
        )

    def test_the_database_keeps_the_defaults_and_the_new_key(self):
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT column_name, column_default, is_nullable FROM information_schema.columns
                WHERE table_name IN ('legacy_documents', 'legacy_imports') AND column_name = 'source_system'
            """)
            columns = cursor.fetchall()
            cursor.execute("""
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'legacy_documents'::regclass AND contype = 'u'
            """)
            keys = {row[0] for row in cursor.fetchall()}
        self.assertEqual(len(columns), 2)
        for _name, default, nullable in columns:
            self.assertIn("'tazman'", default)
            self.assertEqual(nullable, 'NO')
        self.assertEqual(keys, {'legacy_document_source_type_number_unique'})
