"""Importing from any software through the API: tables with a mapping, מבנה אחיד, and two softwares side by side."""
import json

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.customers.models import BusinessCustomer
from apps.legacy_import.models import LegacyDocument, LegacyImport
from apps.legacy_import.tests.helpers import csv_bytes, make_user, xlsx_bytes
from apps.legacy_import.tests.test_api import BASE, ImportFixture, upload as tazman_upload
from apps.legacy_import.tests.test_uniform_reader import DOCUMENTS, export

HEADERS = ['סוג מסמך', 'מספר מסמך', 'תאריך', 'שם לקוח', 'ח"פ', 'סה"כ', 'מע"מ', 'אמצעי תשלום', 'מספר הקצאה']
TABLE = [
    HEADERS,
    # The same types and numbers as the previous software's sample export: 40001, 33001, 70001.
    ['חשבונית מס', '40001', '10/02/2026', 'סטודיו אחר בע"מ', '515555555', '2360', '360', '', '987654321'],
    ['קבלה', '33001', '11/02/2026', 'סטודיו אחר בע"מ', '515555555', '2360', '', 'העברה', ''],
    ['חשבונית מס קבלה', '70001', '12/02/2026', 'הורה מהתוכנה השנייה', '', '100', '15.25', 'Visa', ''],
]


def csv_upload(table=TABLE, name='documents.csv', **kwargs):
    return SimpleUploadedFile(name, csv_bytes(table, **kwargs), content_type='text/csv')


class AnySoftwareFixture(ImportFixture):
    def preview(self, file, fmt='table', source_system='greeninvoice', **extra):
        data = {'file': file, 'format': fmt, 'source_system': source_system}
        for key, value in extra.items():
            data[key] = json.dumps(value) if isinstance(value, (dict, list)) else value
        return self.client.post(f'{BASE}preview/', data)


class ColumnsStepTests(AnySoftwareFixture, APITestCase):
    def test_the_columns_and_the_suggestion_write_nothing(self):
        res = self.client.post(f'{BASE}columns/', {'file': csv_upload()})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual([c['header'] for c in res.data['columns']], HEADERS)
        self.assertEqual(res.data['suggested']['number'], 1)
        self.assertEqual(res.data['suggested']['total'], 5)
        self.assertEqual(res.data['suggested_types']['חשבונית מס קבלה'], 'combined')
        self.assertEqual(res.data['rows'], 3)
        self.assertEqual(res.data['file_kind'], 'csv')
        self.assertTrue(any(f['key'] == 'allocation_number' for f in res.data['fields']))
        self.assertEqual(LegacyImport.objects.count(), 0)

    def test_the_sources_list(self):
        res = self.client.get(f'{BASE}sources/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['sources'][0]['id'], 'tazman')
        self.assertEqual(res.data['formats'], ['tazman', 'table', 'uniform'])


class TableImportTests(AnySoftwareFixture, APITestCase):
    def test_a_csv_previewed_and_committed(self):
        res = self.preview(csv_upload())
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['source_system'], 'greeninvoice')
        summary = res.data['summary']
        self.assertEqual(summary['documents']['total'], 3)
        self.assertEqual(summary['source']['format'], 'table')
        self.assertEqual(summary['source']['columns']['mapping']['allocation_number'], 8)
        self.assertEqual(LegacyDocument.objects.count(), 0)

        result = self.commit(LegacyImport.objects.get(pk=res.data['id'])).data
        self.assertEqual(result['documents']['created'], 3)
        invoice = LegacyDocument.objects.get(doc_type='tax_invoice')
        self.assertEqual(invoice.source_system, 'greeninvoice')
        self.assertEqual((str(invoice.invoice_total), str(invoice.vat_amount), str(invoice.amount_before_vat)),
                         ('2360.00', '360.00', '2000.00'))
        self.assertEqual(invoice.allocation_number, '987654321')
        card = BusinessCustomer.objects.get(company_number='515555555')
        self.assertIn('יובא מתוכנה אחרת (חשבונית ירוקה (morning)): 2 מסמכים', card.notes)
        self.assertEqual(invoice.business_customer, card)
        self.assertEqual(LegacyDocument.objects.get(doc_type='combined').payment_type, 'כרטיס אשראי')

    def test_an_xlsx_with_the_office_s_own_mapping_and_type_names(self):
        table = [['Kind', 'Doc #', 'Issued', 'Customer', 'Amount'],
                 ['Quote-Invoice', 'A-17', '2026-02-10', 'Studio', 50.0]]
        res = self.preview(
            SimpleUploadedFile('docs.xlsx', xlsx_bytes(table)), source_system='אחרת של רו"ח',
            column_mapping={'doc_type': 0, 'number': 1, 'date': 2, 'customer_name': 3, 'total': 4},
            type_values={'Quote-Invoice': 'transaction_invoice'},
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['source_system'], 'אחרת של רו"ח')
        self.commit(LegacyImport.objects.get(pk=res.data['id']))
        doc = LegacyDocument.objects.get()
        self.assertEqual((doc.doc_type, doc.number, doc.original_number), ('transaction_invoice', 17, 'A-17'))

    def test_a_mapping_missing_what_a_document_needs_is_refused(self):
        res = self.preview(csv_upload(), column_mapping={'number': 1})
        self.assertEqual(res.status_code, 400)
        self.assertIn('חסר במיפוי', res.data['error'])
        res = self.preview(csv_upload(), source_system='')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(LegacyImport.objects.count(), 0)

    def test_the_same_file_twice_is_nothing_new(self):
        first = self.preview(csv_upload()).data
        self.commit(LegacyImport.objects.get(pk=first['id']))
        again = self.preview(csv_upload()).data
        self.assertEqual(again['summary']['documents']['already_imported'], 3)
        result = self.commit(LegacyImport.objects.get(pk=again['id'])).data
        self.assertEqual((result['documents']['created'], result['documents']['updated'], result['documents']['unchanged']),
                         (0, 0, 3))
        self.assertEqual((result['customers']['created'], result['customers']['updated']), (0, 0))
        self.assertEqual(LegacyDocument.objects.count(), 3)

    def test_history_only_opens_no_card(self):
        existing = BusinessCustomer.objects.create(first_name='סטודיו', last_name='אחר', company_number='515555555')
        res = self.preview(csv_upload()).data
        result = self.client.post(f"{BASE}{res['id']}/commit/", {'create_customers': False}, format='json').data
        self.assertEqual((result['customers']['created'], result['customers']['updated']), (0, 0))
        self.assertEqual(result['customers']['linked_without_changing_cards'], 1)
        self.assertEqual(BusinessCustomer.objects.count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.notes, '')
        self.assertEqual(LegacyDocument.objects.filter(business_customer=existing).count(), 2)


class TwoSoftwaresTests(AnySoftwareFixture, APITestCase):
    """Audit finding L: a second software's documents with the same numbers used to overwrite the first's."""

    def test_a_second_software_never_overwrites_the_first(self):
        tazman = self.client.post(f'{BASE}preview/', {'file': tazman_upload()}).data
        self.assertEqual(tazman['source_system'], 'tazman')
        self.commit(LegacyImport.objects.get(pk=tazman['id']))
        before = {(d.doc_type, d.number): (d.document_date, d.customer_name, d.invoice_total, d.receipt_total)
                  for d in LegacyDocument.objects.all()}

        other = self.preview(csv_upload()).data
        self.assertEqual(other['summary']['documents']['already_imported'], 0)
        result = self.commit(LegacyImport.objects.get(pk=other['id'])).data

        self.assertEqual((result['documents']['created'], result['documents']['updated']), (3, 0))
        self.assertEqual(LegacyDocument.objects.filter(source_system='tazman').count(), 6)
        self.assertEqual(
            {(d.doc_type, d.number): (d.document_date, d.customer_name, d.invoice_total, d.receipt_total)
             for d in LegacyDocument.objects.filter(source_system='tazman')},
            before,
        )
        self.assertEqual(LegacyDocument.objects.filter(doc_type='tax_invoice', number=40001).count(), 2)

    def test_the_last_numbers_are_per_software(self):
        self.commit(LegacyImport.objects.get(pk=self.client.post(f'{BASE}preview/', {'file': tazman_upload()}).data['id']))
        self.commit(LegacyImport.objects.get(pk=self.preview(csv_upload()).data['id']))
        series = self.client.get(f'{BASE}series/').data['series']
        self.assertEqual(series[0]['source_system'], 'tazman')
        by_source = {(s['source_system'], s['doc_type']): s['count'] for s in series}
        self.assertEqual(by_source[('tazman', 'tax_invoice')], 1)
        self.assertEqual(by_source[('greeninvoice', 'tax_invoice')], 1)

    def test_every_document_says_which_software_issued_it(self):
        self.commit(LegacyImport.objects.get(pk=self.preview(csv_upload()).data['id']))
        rows = self.client.get(f'{BASE}documents/', {'q': '40001'}).data['results']
        self.assertEqual([(r['source'], r['source_system'], r['source_label']) for r in rows],
                         [('legacy', 'greeninvoice', 'חשבונית ירוקה (morning)')])
        self.assertEqual((rows[0]['has_pdf'], rows[0]['pdf_stored']), (False, False))


class UniformImportTests(AnySoftwareFixture, APITestCase):
    def test_the_software_is_read_from_the_ini_and_the_documents_imported(self):
        _files, content = export(DOCUMENTS)
        res = self.preview(SimpleUploadedFile('OPENFRMT.zip', content), fmt='uniform', source_system='')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['source_system'], 'rivhit')
        uniform = res.data['summary']['source']['uniform']
        self.assertEqual((uniform['software'], uniform['records']['C100']), ('Rivhit', 7))
        self.assertEqual(res.data['summary']['documents']['total'], 6)
        result = self.commit(LegacyImport.objects.get(pk=res.data['id'])).data
        self.assertEqual(result['documents']['created'], 6)
        self.assertEqual(LegacyDocument.objects.get(number=41001).linked_document, 'חשבונית מס 40001')
        # The office can say it is another software after all.
        again = self.preview(SimpleUploadedFile('OPENFRMT.zip', content), fmt='uniform', source_system='icount')
        self.assertEqual(again.data['source_system'], 'icount')
        self.assertEqual(again.data['summary']['documents']['already_imported'], 0)


class AnySoftwarePermissionTests(AnySoftwareFixture, APITestCase):
    def test_only_a_manager(self):
        for role in (UserProfile.ROLE_PARTNER, UserProfile.ROLE_WORKER):
            self.client.force_authenticate(make_user(f'{role}-any@test', role))
            with self.subTest(role):
                self.assertEqual(self.client.get(f'{BASE}sources/').status_code, 403)
                self.assertEqual(self.client.post(f'{BASE}columns/', {'file': csv_upload()}).status_code, 403)
                self.assertEqual(self.preview(csv_upload()).status_code, 403)
                pdf = SimpleUploadedFile('40001.pdf', b'%PDF-1.4 test')
                self.assertEqual(self.client.post(f'{BASE}pdfs/', {'files': [pdf]}).status_code, 403)
        self.assertEqual(LegacyImport.objects.count(), 0)
