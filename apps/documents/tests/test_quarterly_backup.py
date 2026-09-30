"""
The quarterly backup (25(ו)(2)): what it holds, what it leaves out, and how it
reaches the bucket — create-only, manifest last. No network: the upload's HTTP
call and the Google token are patched, as in the signed-file backup's tests.
"""
import base64
import gzip
import hashlib
import json
import os
import shutil
import tempfile
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from apps.documents.models import DocumentPayment, DocumentSeries, FormalDocument, SignedOriginal
from apps.documents.quarterly_backup import (
    BackupInputError,
    BackupNotConfigured,
    FISCAL_DATA,
    MANIFEST,
    parse_quarter,
    previous_quarter,
    quarter_bounds,
    quarterly_bucket,
    run_daily_backup,
    run_quarterly_backup,
)
from apps.documents.tests.test_register import RegisterFixture
from apps.legacy_import.models import LegacyImport

NOW = datetime(2026, 10, 1, 7, 30, tzinfo=dt_timezone.utc)  # 10:30 in Israel, the first week of Q4
POST = 'apps.documents.signing.backup.requests.post'
TOKEN = 'apps.documents.signing.kms.access_token'
CRON = '/api/v1/documents/cron/quarterly-backup/'
DAILY_CRON = '/api/v1/documents/cron/daily-backup/'


def google(status_code, body=None):
    response = MagicMock(status_code=status_code)
    response.json.return_value = body or {}
    return response


def uploaded_metadata(call):
    body = call.kwargs['data']
    return json.loads(body.split(b'\r\n\r\n', 1)[1].split(b'\r\n--', 1)[0])


class QuarterTests(TestCase):
    def test_the_quarter_that_ended_last(self):
        self.assertEqual(previous_quarter(date(2026, 10, 1)), (2026, 3))
        self.assertEqual(previous_quarter(date(2027, 1, 5)), (2026, 4))
        self.assertEqual(quarter_bounds(2026, 3), (date(2026, 7, 1), date(2026, 9, 30)))
        self.assertEqual(quarter_bounds(2026, 4), (date(2026, 10, 1), date(2026, 12, 31)))

    def test_a_quarter_is_read_or_refused(self):
        self.assertEqual(parse_quarter('2026-Q2', date(2026, 10, 1)), (2026, 2))
        self.assertEqual(parse_quarter(None, date(2026, 10, 1)), (2026, 3))
        with self.assertRaises(BackupInputError):
            parse_quarter('2026-5', date(2026, 10, 1))
        with self.assertRaises(BackupInputError):
            parse_quarter('2027-Q1', date(2026, 10, 1))


class BackupFixture(RegisterFixture):
    def setUp(self):
        super().setUp()
        self.out = tempfile.mkdtemp(prefix='kogo-qb-test-')
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        DocumentSeries.objects.create(series='IR', year=2026, counter=1)
        self.lesson_receipt('IR-2026-000001', child=self.kid)
        self.receipt = FormalDocument.objects.create(
            document_number='RC-2026-000001', document_type='receipt', client_type='existing', child=self.kid,
            document_date=date(2026, 8, 12), subtotal=Decimal('100.00'), total_amount=Decimal('100.00'),
        )
        DocumentPayment.objects.create(document=self.receipt, payment_method='cash', amount=Decimal('100.00'))
        self.signed = SignedOriginal.objects.create(
            number='RC-2026-000001', kind='formal', source_id=str(self.receipt.pk), pdf=b'%PDF-signed-bytes',
            sha256='a' * 64, size=17, signed_at=datetime(2026, 8, 12, 9, tzinfo=dt_timezone.utc),
        )
        LegacyImport.objects.create(file_name='old.xls', sha256='b' * 64, rows=[{'secret': 'raw row'}],
                                    summary={'customers': {'total': 1}})

    def records(self, path):
        with gzip.open(path, 'rt', encoding='utf-8') as handle:
            return [json.loads(line) for line in handle]


class LocalBackupTests(BackupFixture, TestCase):
    def test_the_folder_holds_every_part_and_the_manifest_matches_them(self):
        result = run_quarterly_backup('2026-Q3', out_dir=self.out, bucket_name='', now=NOW)

        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['prefix'], 'books/quarterly/2026-Q3/20261001T103000')
        folder = result['local_dir']
        names = sorted(os.listdir(folder))
        self.assertEqual(names, sorted([
            FISCAL_DATA, MANIFEST, 'README.txt',
            'register-2026-07.csv', 'register-2026-08.csv', 'register-2026-09.csv',
            'uniform-2026-07.zip', 'uniform-2026-08.zip', 'uniform-2026-09.zip',
        ]))
        with open(os.path.join(folder, MANIFEST), encoding='utf-8') as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest['quarter'], '2026-Q3')
        self.assertEqual(manifest['period'], {'start': '2026-07-01', 'end': '2026-09-30'})
        self.assertEqual(manifest['errors'], [])
        for part in manifest['parts']:
            with open(os.path.join(folder, part['name']), 'rb') as handle:
                payload = handle.read()
            self.assertEqual(part['sha256'], hashlib.sha256(payload).hexdigest(), part['name'])
            self.assertEqual(part['size'], len(payload))
        tables = manifest['parts'][0]['tables']
        self.assertEqual(tables['documents.formaldocument'], 1)
        self.assertEqual(tables['documents.documentpayment'], 1)
        self.assertEqual(tables['customers.invoice'], 1)
        self.assertEqual(tables['documents.signedoriginal'], 1)

        records = self.records(os.path.join(folder, FISCAL_DATA))
        by_model = {}
        for record in records:
            by_model.setdefault(record['model'], []).append(record)
        self.assertEqual(sum(tables.values()), len(records))
        self.assertEqual(by_model['documents.formaldocument'][0]['fields']['document_number'], 'RC-2026-000001')

    def test_what_the_backup_leaves_out(self):
        result = run_quarterly_backup('2026-Q3', out_dir=self.out, bucket_name='', now=NOW)
        records = self.records(os.path.join(result['local_dir'], FISCAL_DATA))
        by_model = {record['model']: record['fields'] for record in records}
        # The signed PDF bytes live in the locked signed-documents bucket, not here.
        self.assertNotIn('pdf', by_model['documents.signedoriginal'])
        self.assertEqual(by_model['documents.signedoriginal']['sha256'], 'a' * 64)
        # The raw file an import was read from.
        self.assertNotIn('rows', by_model['legacy_import.legacyimport'])
        self.assertNotIn('summary', by_model['legacy_import.legacyimport'])
        # A child is a name on a document, nothing more.
        self.assertEqual(set(by_model['customers.child']), {'family', 'first_name', 'last_name'})
        self.assertNotIn('notes', by_model['customers.family'])

    def test_the_register_of_the_month_is_in_the_folder(self):
        result = run_quarterly_backup('2026-Q3', out_dir=self.out, bucket_name='', now=NOW)
        with open(os.path.join(result['local_dir'], 'register-2026-08.csv'), encoding='utf-8-sig') as handle:
            text = handle.read()
        self.assertIn('IR-2026-000001', text)
        self.assertIn('הכנסה ללא מסמך', text)

    def test_a_month_that_fails_is_reported_and_the_rest_is_kept(self):
        with patch('apps.documents.uniform_export.build_uniform_export', side_effect=RuntimeError('boom')):
            result = run_quarterly_backup('2026-Q3', out_dir=self.out, bucket_name='', now=NOW)
        self.assertFalse(result['ok'])
        self.assertEqual(len(result['errors']), 3)
        self.assertTrue(os.path.exists(os.path.join(result['local_dir'], FISCAL_DATA)))

    def test_neither_a_bucket_nor_a_directory_is_refused(self):
        with self.assertRaises(BackupNotConfigured):
            run_quarterly_backup('2026-Q3', bucket_name='', now=NOW)
        with override_settings(SIGNING_QUARTERLY_BACKUP_BUCKET='', SIGNING_BACKUP_BUCKET=''), \
                self.assertRaises(CommandError):
            call_command('quarterly_backup', '--quarter', '2026-Q3')

    def test_the_command_writes_the_folder(self):
        with override_settings(SIGNING_QUARTERLY_BACKUP_BUCKET='', SIGNING_BACKUP_BUCKET=''):
            call_command('quarterly_backup', '--quarter', '2026-Q3', '--out', self.out, stdout=open(os.devnull, 'w'))
        quarter = os.path.join(self.out, 'books', 'quarterly', '2026-Q3')
        (folder,) = os.listdir(quarter)
        self.assertIn(MANIFEST, os.listdir(os.path.join(quarter, folder)))


@patch(TOKEN, return_value='test-access-token')
class BucketBackupTests(BackupFixture, TestCase):
    def test_every_part_is_created_once_with_its_md5_and_the_manifest_goes_last(self, _token):
        with patch(POST, return_value=google(200)) as post:
            result = run_quarterly_backup('2026-Q3', bucket_name='kogo-quarterly-test', now=NOW)

        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['local_dir'], '')
        self.assertEqual(post.call_count, len(result['parts']))
        names = []
        for call in post.call_args_list:
            self.assertIn('/b/kogo-quarterly-test/o', call.args[0])
            self.assertEqual(call.kwargs['params'], {'uploadType': 'multipart', 'ifGenerationMatch': '0'})
            self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer test-access-token')
            metadata = uploaded_metadata(call)
            payload = call.kwargs['data'].split(b'\r\n\r\n', 2)[2].rsplit(b'\r\n--', 1)[0]
            self.assertEqual(metadata['md5Hash'], base64.b64encode(hashlib.md5(payload).digest()).decode())
            self.assertEqual(metadata['metadata']['sha256'], hashlib.sha256(payload).hexdigest())
            self.assertEqual(metadata['metadata']['quarter'], '2026-Q3')
            names.append(metadata['name'])
        folder = 'books/quarterly/2026-Q3/20261001T103000'
        self.assertTrue(all(name.startswith(f'{folder}/') for name in names))
        self.assertEqual(names[0], f'{folder}/{FISCAL_DATA}')
        self.assertEqual(names[-1], f'{folder}/{MANIFEST}')
        # The fiscal part goes up as gzip, not as a PDF.
        self.assertEqual(uploaded_metadata(post.call_args_list[0])['contentType'], 'application/gzip')

    def test_a_refused_bucket_stops_and_leaves_no_manifest(self, _token):
        refused = google(403, {'error': {'status': 'PERMISSION_DENIED'}})
        with patch(POST, return_value=refused) as post:
            result = run_quarterly_backup('2026-Q3', bucket_name='kogo-quarterly-test', now=NOW)
        self.assertEqual(post.call_count, 1)
        self.assertFalse(result['ok'])
        self.assertEqual(result['uploaded'], [])
        self.assertIn('PERMISSION_DENIED', result['errors'][0])
        self.assertNotIn('test-access-token', ' '.join(result['errors']))

    def test_a_name_already_taken_is_an_error_not_a_success(self, _token):
        with patch(POST, return_value=google(412)):
            result = run_quarterly_backup('2026-Q3', bucket_name='kogo-quarterly-test', now=NOW)
        self.assertFalse(result['ok'])
        self.assertNotIn(MANIFEST, result['uploaded'])
        self.assertIn('already exists', result['errors'][0])


@patch(TOKEN, return_value='test-access-token')
class CronEndpointTests(BackupFixture, APITestCase):
    def test_without_the_token_it_answers_401(self, _token):
        with override_settings(CRON_TOKEN='cron-secret', SIGNING_QUARTERLY_BACKUP_BUCKET='kogo-quarterly-test'):
            self.assertEqual(self.client.get(CRON).status_code, 401)
            self.assertEqual(self.client.get(CRON, {'token': 'wrong'}).status_code, 401)

    def test_without_a_bucket_it_answers_409(self, _token):
        with override_settings(CRON_TOKEN='cron-secret', SIGNING_QUARTERLY_BACKUP_BUCKET='', SIGNING_BACKUP_BUCKET=''):
            res = self.client.get(CRON, HTTP_X_CRON_TOKEN='cron-secret')
        self.assertEqual(res.status_code, 409)

    def test_with_the_token_it_backs_up_to_the_bucket(self, _token):
        with override_settings(CRON_TOKEN='cron-secret', SIGNING_QUARTERLY_BACKUP_BUCKET='kogo-quarterly-test'), \
                patch(POST, return_value=google(200)) as post:
            res = self.client.post(f'{CRON}?quarter=2026-Q3', HTTP_AUTHORIZATION='Bearer cron-secret')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data['ok'])
        self.assertEqual(res.data['quarter'], '2026-Q3')
        self.assertEqual(post.call_count, len(res.data['parts']))

    def test_a_bad_quarter_is_a_400(self, _token):
        with override_settings(CRON_TOKEN='cron-secret', SIGNING_QUARTERLY_BACKUP_BUCKET='kogo-quarterly-test'):
            res = self.client.get(CRON, {'quarter': 'Q9', 'token': 'cron-secret'})
        self.assertEqual(res.status_code, 400)


class BucketChoiceTests(TestCase):
    def test_its_own_bucket_else_the_signed_files_bucket(self):
        with override_settings(SIGNING_QUARTERLY_BACKUP_BUCKET='books-only', SIGNING_BACKUP_BUCKET='signed'):
            self.assertEqual(quarterly_bucket(), 'books-only')
        with override_settings(SIGNING_QUARTERLY_BACKUP_BUCKET='', SIGNING_BACKUP_BUCKET='signed'):
            self.assertEqual(quarterly_bucket(), 'signed')
        with override_settings(SIGNING_QUARTERLY_BACKUP_BUCKET='', SIGNING_BACKUP_BUCKET=''):
            self.assertEqual(quarterly_bucket(), '')


class DailyBackupTests(BackupFixture, TestCase):
    def test_the_daily_folder_holds_the_whole_fiscal_data(self):
        result = run_daily_backup(out_dir=self.out, bucket_name='', now=NOW)

        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['day'], '2026-10-01')
        self.assertEqual(result['prefix'], 'books/daily/2026-10-01/103000')
        folder = result['local_dir']
        self.assertEqual(sorted(os.listdir(folder)), sorted([FISCAL_DATA, MANIFEST, 'README.txt']))
        with open(os.path.join(folder, MANIFEST), encoding='utf-8') as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest['kind'], 'kogo-daily-backup')
        self.assertEqual(manifest['day'], '2026-10-01')
        tables = manifest['parts'][0]['tables']
        self.assertEqual(tables['documents.formaldocument'], 1)
        self.assertEqual(tables['customers.invoice'], 1)
        records = self.records(os.path.join(folder, FISCAL_DATA))
        self.assertEqual(sum(tables.values()), len(records))
        by_model = {record['model']: record['fields'] for record in records}
        # The same leave-outs as the quarter: no PDF bytes, no raw import file.
        self.assertNotIn('pdf', by_model['documents.signedoriginal'])
        self.assertNotIn('rows', by_model['legacy_import.legacyimport'])

    def test_neither_a_bucket_nor_a_directory_is_refused(self):
        with self.assertRaises(BackupNotConfigured):
            run_daily_backup(bucket_name='', now=NOW)
        with override_settings(SIGNING_QUARTERLY_BACKUP_BUCKET='', SIGNING_BACKUP_BUCKET=''), \
                self.assertRaises(CommandError):
            call_command('daily_backup')

    @patch(TOKEN, return_value='test-access-token')
    def test_to_the_signed_files_bucket_under_books_manifest_last(self, _token):
        with override_settings(SIGNING_QUARTERLY_BACKUP_BUCKET='', SIGNING_BACKUP_BUCKET='kogomelo-signed-test'), \
                patch(POST, return_value=google(200)) as post:
            result = run_daily_backup(now=NOW)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['bucket'], 'kogomelo-signed-test')
        names = [uploaded_metadata(call)['name'] for call in post.call_args_list]
        self.assertEqual(names, [
            f'books/daily/2026-10-01/103000/{FISCAL_DATA}',
            'books/daily/2026-10-01/103000/README.txt',
            f'books/daily/2026-10-01/103000/{MANIFEST}',
        ])
        for call in post.call_args_list:
            self.assertIn('/b/kogomelo-signed-test/o', call.args[0])
            self.assertEqual(call.kwargs['params'], {'uploadType': 'multipart', 'ifGenerationMatch': '0'})

    @patch(TOKEN, return_value='test-access-token')
    def test_a_refused_upload_leaves_no_manifest(self, _token):
        with patch(POST, return_value=google(403, {'error': {'status': 'PERMISSION_DENIED'}})) as post:
            result = run_daily_backup(bucket_name='kogomelo-signed-test', now=NOW)
        self.assertEqual(post.call_count, 1)
        self.assertFalse(result['ok'])
        self.assertEqual(result['uploaded'], [])


@patch(TOKEN, return_value='test-access-token')
class DailyCronEndpointTests(BackupFixture, APITestCase):
    def test_without_the_token_it_answers_401(self, _token):
        with override_settings(CRON_TOKEN='cron-secret', SIGNING_BACKUP_BUCKET='kogomelo-signed-test'):
            self.assertEqual(self.client.get(DAILY_CRON).status_code, 401)

    def test_without_a_bucket_it_answers_409(self, _token):
        with override_settings(CRON_TOKEN='cron-secret', SIGNING_QUARTERLY_BACKUP_BUCKET='', SIGNING_BACKUP_BUCKET=''):
            res = self.client.get(DAILY_CRON, HTTP_X_CRON_TOKEN='cron-secret')
        self.assertEqual(res.status_code, 409)

    def test_with_the_token_it_copies_the_books(self, _token):
        with override_settings(CRON_TOKEN='cron-secret', SIGNING_QUARTERLY_BACKUP_BUCKET='',
                               SIGNING_BACKUP_BUCKET='kogomelo-signed-test'), \
                patch(POST, return_value=google(200)) as post:
            res = self.client.get(DAILY_CRON, HTTP_AUTHORIZATION='Bearer cron-secret')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data['ok'])
        self.assertEqual(post.call_count, 3)

    def test_a_failed_upload_is_a_502(self, _token):
        with override_settings(CRON_TOKEN='cron-secret', SIGNING_BACKUP_BUCKET='kogomelo-signed-test'), \
                patch(POST, return_value=google(500)):
            res = self.client.get(DAILY_CRON, HTTP_X_CRON_TOKEN='cron-secret')
        self.assertEqual(res.status_code, 502)
        self.assertFalse(res.data['ok'])
