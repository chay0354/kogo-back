"""
The archive's cutoff — the moment signing went on — and the copy of every signed file in the locked bucket.

Once DOCUMENT_SIGNING_ENABLED is on, a new document is signed at issue as its
original; the archive must never take it first. And every signed file, original
or archive copy, is also written once, create-only, to the backup bucket.
"""
import base64
import hashlib
import json
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework.test import APITestCase

from apps.documents.models import SignedOriginal
from apps.documents.signing.archive import (
    ArchiveNeedsCutoff, archive_candidates, archive_status, run_archive_batch, sign_archive,
)
from apps.documents.signing.backup import backup_pending, object_name
from apps.documents.signing.views import ARCHIVE_NEEDS_CUTOFF
from apps.documents.tests.signing_support import archive_on
from apps.documents.tests.test_signed_archive import IR, ArchiveFixture

CUTOFF = '2026-09-24T09:55:00+03:00'


def archive_with_signing_on(**extra):
    """Both switches on, as in production after the owner's letter."""
    return archive_on(DOCUMENT_SIGNING_ENABLED=True, **extra)


class CutoffTests(ArchiveFixture, TestCase):
    def created(self, obj, moment: str):
        type(obj).objects.filter(pk=obj.pk).update(created_at=moment)

    def test_with_signing_on_and_no_cutoff_the_archive_refuses_to_run(self):
        self.lesson_receipt('IR-2026-000001')
        with archive_with_signing_on():
            with self.assertRaises(ArchiveNeedsCutoff):
                run_archive_batch(limit=5)
            status = archive_status()
        self.assertEqual(status['kinds'], [])
        self.assertTrue(status['blocked'])
        self.assertFalse(SignedOriginal.objects.exists())

    def test_a_cutoff_without_its_offset_or_unreadable_is_refused(self):
        for bad in ('2026-09-24T09:55:00', 'yesterday'):
            with archive_with_signing_on(SIGNING_ARCHIVE_ISSUED_BEFORE=bad):
                with self.assertRaises(ArchiveNeedsCutoff):
                    run_archive_batch(limit=5)

    def test_only_documents_created_before_signing_went_on_are_taken(self):
        before = self.lesson_receipt('IR-2026-000001')
        after = self.lesson_receipt('IR-2026-000002')
        self.created(before, '2026-09-24T09:00:00+03:00')
        self.created(after, '2026-09-24T10:30:00+03:00')
        with archive_with_signing_on(SIGNING_ARCHIVE_ISSUED_BEFORE=CUTOFF):
            self.assertEqual([i.pk for i in archive_candidates(IR)], [before.pk])
            self.assertIsNone(sign_archive(IR, after))
            result = run_archive_batch(limit=10)
            status = archive_status()
        self.assertEqual(result['signed'], 1)
        self.assertEqual(SignedOriginal.objects.get().source_id, str(before.pk))
        ir = next(k for k in status['kinds'] if k['kind'] == IR)
        self.assertEqual((ir['eligible'], ir['archived'], ir['remaining']), (1, 1, 0))
        self.assertEqual(status['issued_before'], '2026-09-24T09:55:00+03:00')

    def test_with_signing_off_no_cutoff_is_needed(self):
        self.lesson_receipt('IR-2026-000001')
        with archive_on():
            self.assertEqual(run_archive_batch(limit=5)['signed'], 1)


class CutoffEndpointTests(ArchiveFixture, APITestCase):
    def test_the_run_answers_409_with_the_reason(self):
        self.client.force_authenticate(self.manager)
        with archive_with_signing_on():
            response = self.client.post('/api/v1/documents/signing/archive/run/', {'limit': 5}, format='json')
            status_response = self.client.get('/api/v1/documents/signing/archive/status/')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['error'], ARCHIVE_NEEDS_CUTOFF)
        self.assertEqual(status_response.status_code, 200)
        self.assertTrue(status_response.data['blocked'])


def google(status_code: int, body: dict | None = None):
    response = MagicMock(status_code=status_code)
    response.json.return_value = body or {}
    return response


@patch('apps.documents.signing.kms.access_token', return_value='test-access-token')
class BackupTests(ArchiveFixture, APITestCase):
    def archived(self, number='IR-2026-000001'):
        lesson = self.lesson_receipt(number)
        with archive_on():
            return sign_archive(IR, lesson)

    def test_without_a_bucket_nothing_is_copied(self, _token):
        self.archived()
        with archive_on():
            self.assertEqual(backup_pending(), {'disabled': True})

    def test_a_signed_file_is_copied_once_create_only_with_its_md5(self, _token):
        row = self.archived()
        with archive_on(SIGNING_BACKUP_BUCKET='kogo-test-bucket'), \
                patch('apps.documents.signing.backup.requests.post', return_value=google(200)) as post:
            summary = backup_pending()
            again = backup_pending()
        self.assertEqual((summary['copied'], summary['failed'], summary['remaining']), (1, 0, 0))
        self.assertEqual(again['copied'], 0)
        self.assertEqual(post.call_count, 1)
        url = post.call_args.args[0]
        kwargs = post.call_args.kwargs
        self.assertIn('/b/kogo-test-bucket/o', url)
        self.assertEqual(kwargs['params'], {'uploadType': 'multipart', 'ifGenerationMatch': '0'})
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer test-access-token')
        body = kwargs['data']
        pdf = bytes(SignedOriginal.objects.get(pk=row.pk).pdf)
        self.assertIn(pdf, body)
        metadata = json.loads(body.split(b'\r\n\r\n', 1)[1].split(b'\r\n--', 1)[0])
        self.assertEqual(metadata['name'], object_name(row))
        self.assertTrue(metadata['name'].startswith('archive/'))
        self.assertEqual(metadata['md5Hash'], base64.b64encode(hashlib.md5(pdf).digest()).decode())
        self.assertEqual(metadata['metadata']['sha256'], row.sha256)
        self.assertIsNotNone(SignedOriginal.objects.get(pk=row.pk).backup_at)

    def test_a_copy_already_in_the_bucket_counts_as_copied(self, _token):
        row = self.archived()
        with archive_on(SIGNING_BACKUP_BUCKET='kogo-test-bucket'), \
                patch('apps.documents.signing.backup.requests.post', return_value=google(412)):
            self.assertEqual(backup_pending()['copied'], 1)
        self.assertIsNotNone(SignedOriginal.objects.get(pk=row.pk).backup_at)

    def test_a_refused_bucket_stops_the_run_and_records_why(self, _token):
        first = self.archived('IR-2026-000001')
        self.archived('IR-2026-000002')
        refused = google(403, {'error': {'status': 'PERMISSION_DENIED'}})
        with archive_on(SIGNING_BACKUP_BUCKET='kogo-test-bucket'), \
                patch('apps.documents.signing.backup.requests.post', return_value=refused) as post:
            summary = backup_pending()
        self.assertEqual(post.call_count, 1)
        self.assertTrue(summary['stopped'])
        self.assertEqual((summary['copied'], summary['remaining']), (0, 2))
        row = SignedOriginal.objects.get(pk=first.pk)
        self.assertIsNone(row.backup_at)
        self.assertIn('PERMISSION_DENIED', row.backup_error)
        self.assertNotIn('test-access-token', row.backup_error)

    def test_one_failed_file_does_not_stop_the_others(self, _token):
        self.archived('IR-2026-000001')
        self.archived('IR-2026-000002')
        with archive_on(SIGNING_BACKUP_BUCKET='kogo-test-bucket'), \
                patch('apps.documents.signing.backup.requests.post',
                      side_effect=[google(500, {'error': {'status': 'INTERNAL'}}), google(200)]):
            summary = backup_pending()
        self.assertEqual((summary['copied'], summary['failed'], summary['remaining']), (1, 1, 1))

    def test_the_cron_copies_after_signing_and_a_backup_crash_never_fails_it(self, _token):
        self.archived()
        with archive_on(SIGNING_BACKUP_BUCKET='kogo-test-bucket', CRON_TOKEN='cron-test'), \
                patch('apps.documents.signing.backup.requests.post', return_value=google(200)):
            response = self.client.get('/api/v1/documents/cron/sign-pending/', HTTP_X_CRON_TOKEN='cron-test')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['backup']['copied'], 1)

        self.archived('IR-2026-000002')
        with archive_on(SIGNING_BACKUP_BUCKET='kogo-test-bucket', CRON_TOKEN='cron-test'), \
                patch('apps.documents.signing.backup.backup_pending', side_effect=RuntimeError('boom')):
            response = self.client.get('/api/v1/documents/cron/sign-pending/', HTTP_X_CRON_TOKEN='cron-test')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['backup'], {'error': 'RuntimeError'})

    def test_the_archive_status_shows_the_backup(self, _token):
        self.archived()
        self.client.force_authenticate(self.manager)
        with archive_on(SIGNING_BACKUP_BUCKET='kogo-test-bucket'):
            data = self.client.get('/api/v1/documents/signing/archive/status/').json()
        self.assertEqual(data['backup'], {'enabled': True, 'copied': 0, 'pending': 1, 'last_error': ''})
