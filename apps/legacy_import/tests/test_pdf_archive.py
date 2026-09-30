"""
The old software's PDFs: matched by number, fingerprinted, and written create-only
to the locked bucket. No network: the upload's HTTP call and the Google token are
patched, as in apps/documents/tests/test_quarterly_backup.py.
"""
import hashlib
import io
import json
import zipfile
from datetime import date
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.legacy_import import pdf_archive
from apps.legacy_import.models import LegacyDocument
from apps.legacy_import.tests.test_api import BASE, ImportFixture

POST = 'apps.documents.signing.backup.requests.post'
TOKEN = 'apps.documents.signing.kms.access_token'
BUCKET = 'kogo-signed-test'


def google(status_code, body=None):
    response = MagicMock(status_code=status_code)
    response.json.return_value = body or {}
    return response


def uploaded_metadata(call):
    body = call.kwargs['data']
    return json.loads(body.split(b'\r\n\r\n', 1)[1].split(b'\r\n--', 1)[0])


def pdf(text='x'):
    return f'%PDF-1.4\n% {text}\n%%EOF\n'.encode()


def zipped(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return SimpleUploadedFile('pdfs.zip', buffer.getvalue(), content_type='application/zip')


class PdfFixture(ImportFixture):
    def setUp(self):
        super().setUp()

        def legacy(source, doc_type, number, when=date(2025, 3, 1), **extra):
            return LegacyDocument.objects.create(
                source_system=source, original_type=doc_type, doc_type=doc_type, number=number,
                document_date=when, **extra,
            )

        self.invoice = legacy('tazman', 'tax_invoice', 40001)
        self.receipt_same_number = legacy('tazman', 'receipt', 40001)
        self.receipt = legacy('tazman', 'receipt', 33001, when=date(2024, 12, 30))
        self.other_software = legacy('greeninvoice', 'tax_invoice', 33001)
        self.printed = legacy('greeninvoice', 'transaction_invoice', 15, original_number='INV-0015')

    def send(self, source='tazman', **files):
        data = {'source_system': source}
        data.update(files)
        return self.client.post(f'{BASE}pdfs/', data)

    def statuses(self, res):
        return {entry['file']: entry['status'] for entry in res.data['files']}


@override_settings(SIGNING_BACKUP_BUCKET='')
class WithoutBucketTests(PdfFixture, APITestCase):
    def test_matched_by_number_and_type_and_only_the_fingerprint_is_kept(self):
        res = self.send(file=zipped({
            'קבלות/33001.pdf': pdf('receipt'),
            'חשבונית מס 40001.pdf': pdf('invoice'),
            '40001.pdf': pdf('which one?'),
            '99999.pdf': pdf('nobody'),
            'notes.txt': b'not a pdf',
            'copy/33001-copy.pdf': pdf('receipt'),
        }))
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(res.data['bucket_configured'])
        self.assertEqual(self.statuses(res), {
            'קבלות/33001.pdf': 'fingerprinted',
            'חשבונית מס 40001.pdf': 'fingerprinted',
            '40001.pdf': 'ambiguous',
            '99999.pdf': 'unmatched',
            'notes.txt': 'rejected',
            'copy/33001-copy.pdf': 'duplicate',
        })
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.pdf_sha256, hashlib.sha256(pdf('receipt')).hexdigest())
        self.assertEqual((self.receipt.pdf_size, self.receipt.pdf_object), (len(pdf('receipt')), ''))
        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.pdf_sha256)
        # The other software's 33001 is another document.
        self.other_software.refresh_from_db()
        self.assertEqual(self.other_software.pdf_sha256, '')
        ambiguous = next(e for e in res.data['files'] if e['status'] == 'ambiguous')
        self.assertEqual({c['doc_type'] for c in ambiguous['candidates']}, {'tax_invoice', 'receipt'})

    def test_sending_the_same_files_again_changes_nothing_and_a_different_pdf_is_a_conflict(self):
        self.send(files=[SimpleUploadedFile('33001.pdf', pdf('receipt'))])
        again = self.send(files=[SimpleUploadedFile('33001.pdf', pdf('receipt')),
                                 SimpleUploadedFile('receipt 33001 scan.pdf', pdf('another scan'))])
        self.assertEqual(again.data['counts'], {'already': 1, 'duplicate': 1})
        other = self.send(files=[SimpleUploadedFile('33001 (1).pdf', pdf('another scan'))])
        self.assertEqual(other.data['counts'], {'conflict': 1})
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.pdf_sha256, hashlib.sha256(pdf('receipt')).hexdigest())

    def test_the_printed_number_dates_in_names_and_the_chosen_software(self):
        res = self.send('greeninvoice', files=[
            SimpleUploadedFile('INV-0015.pdf', pdf('printed')),
            SimpleUploadedFile('invoice_33001_2025-03-01.pdf', pdf('dated')),
        ])
        self.assertEqual(res.data['counts'], {'fingerprinted': 2})
        self.printed.refresh_from_db()
        self.other_software.refresh_from_db()
        self.assertTrue(self.printed.pdf_sha256 and self.other_software.pdf_sha256)

    def test_the_request_limit(self):
        big = SimpleUploadedFile('big.zip', b'\0' * (pdf_archive.MAX_UPLOAD_BYTES + 1))
        res = self.send(file=big)
        self.assertEqual(res.status_code, 400)
        self.assertIn('4.3MB', res.data['error'])
        self.assertEqual(self.send().status_code, 400)


@override_settings(SIGNING_BACKUP_BUCKET=BUCKET)
@patch(TOKEN, return_value='test-access-token')
class WithBucketTests(PdfFixture, APITestCase):
    def test_the_bytes_go_to_the_bucket_create_only_and_the_database_keeps_the_fingerprint(self, _token):
        with patch(POST, return_value=google(200)) as post:
            res = self.send(files=[SimpleUploadedFile('33001.pdf', pdf('receipt'))])
        self.assertEqual(res.data['counts'], {'stored': 1})
        call = post.call_args
        self.assertIn(f'/b/{BUCKET}/o', call.args[0])
        self.assertEqual(call.kwargs['params'], {'uploadType': 'multipart', 'ifGenerationMatch': '0'})
        metadata = uploaded_metadata(call)
        self.assertEqual(metadata['name'], 'legacy/tazman/2024/receipt/33001.pdf')
        self.assertEqual(metadata['contentType'], 'application/pdf')
        self.assertEqual(metadata['metadata']['sha256'], hashlib.sha256(pdf('receipt')).hexdigest())
        self.assertIn(pdf('receipt'), call.kwargs['data'])
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.pdf_object, 'legacy/tazman/2024/receipt/33001.pdf')
        # The row holds the fingerprint, never the bytes.
        self.assertFalse(any(isinstance(getattr(self.receipt, f.attname), (bytes, memoryview))
                             for f in LegacyDocument._meta.concrete_fields))

    def test_a_fingerprint_kept_before_the_bucket_existed_is_sent_now(self, _token):
        LegacyDocument.objects.filter(pk=self.receipt.pk).update(
            pdf_sha256=hashlib.sha256(pdf('receipt')).hexdigest(), pdf_size=10,
        )
        with patch(POST, return_value=google(200)) as post:
            res = self.send(files=[SimpleUploadedFile('33001.pdf', pdf('receipt'))])
        self.assertEqual(res.data['counts'], {'stored': 1})
        self.assertEqual(post.call_count, 1)
        with patch(POST, return_value=google(200)) as post:
            again = self.send(files=[SimpleUploadedFile('33001.pdf', pdf('receipt'))])
        self.assertEqual(again.data['counts'], {'already': 1})
        post.assert_not_called()

    def test_a_name_already_taken_in_the_bucket_counts_as_stored(self, _token):
        with patch(POST, return_value=google(412)):
            res = self.send(files=[SimpleUploadedFile('33001.pdf', pdf('receipt'))])
        self.assertEqual(res.data['counts'], {'stored': 1})

    def test_the_bucket_out_of_reach_stops_and_says_how_many_are_left(self, _token):
        refused = google(403, {'error': {'status': 'PERMISSION_DENIED'}})
        with patch(POST, return_value=refused):
            res = self.send(files=[SimpleUploadedFile('33001.pdf', pdf('a')),
                                   SimpleUploadedFile('חשבונית מס 40001.pdf', pdf('b'))])
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['stopped'])
        self.assertEqual(res.data['remaining'], 2)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.pdf_sha256, '')

    def test_one_refusal_does_not_stop_the_others(self, _token):
        with patch(POST, side_effect=[google(500), google(200)]):
            res = self.send(files=[SimpleUploadedFile('33001.pdf', pdf('a')),
                                   SimpleUploadedFile('חשבונית מס 40001.pdf', pdf('b'))])
        self.assertEqual(res.data['counts'], {'failed': 1, 'stored': 1})
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.pdf_sha256, '')


class NameTests(APITestCase):
    def test_numbers_and_type_hints_in_names(self):
        self.assertEqual(pdf_archive.numbers_in('invoice_40001_2025-01-05.pdf'), [40001])
        self.assertEqual(pdf_archive.numbers_in('folder/05.01.2025 קבלה 33001.pdf'), [33001])
        self.assertEqual(pdf_archive.type_hint('חשבונית מס קבלה 70001.pdf'), 'combined')
        self.assertEqual(pdf_archive.type_hint('Credit note 41001.pdf'), 'credit_invoice')
        self.assertEqual(pdf_archive.type_hint('400/33001.pdf'), 'receipt')
        self.assertEqual(pdf_archive.type_hint('33001.pdf'), '')
