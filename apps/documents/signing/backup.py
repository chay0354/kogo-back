"""
A second, locked home for every signed file: a Google Cloud Storage bucket.

The signed files live in the database (signed_originals.pdf), which is what
the office reads. A database can be wiped by someone holding its password, and
the originals must survive seven years (הוראות ניהול פנקסי חשבונות, סעיף 25).
So every signed file — original or archive copy — is also written to a bucket
in me-west1 whose retention policy keeps each object, unchanged, for ten years:
no one can delete or replace it before then, not the service account that
writes it (roles/storage.objectCreator on that one bucket: create only) and not
the project's owner once the policy is locked.

Plain HTTPS through ``requests`` and the signing service account's token
(kms.access_token), like the rest of the package. One multipart upload per
file, created only if the name is free (ifGenerationMatch=0): a copy is never
overwritten, and a 412 answer means it is already there. The upload carries the
file's MD5, so Google refuses a copy that arrived damaged.

The copy never holds anything up. The sign-pending cron copies what is not yet
copied, a few per run; a failure is recorded on the row and tried again on the
next run. Nothing here is on the path of a charge or a mail.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
import uuid

import requests
from django.utils import timezone

from apps.documents.models import SignedOriginal
from apps.documents.signing import SigningUnavailable

logger = logging.getLogger(__name__)

UPLOAD_URL = 'https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o'
TIMEOUT = (3.05, 20)
DEFAULT_LIMIT = 20
DEFAULT_TIME_BUDGET = 6.0


def bucket() -> str:
    from django.conf import settings

    return (getattr(settings, 'SIGNING_BACKUP_BUCKET', '') or '').strip()


def object_name(row: SignedOriginal) -> str:
    """original/2026/ir/IR-2026-000123.pdf — what it is, the year it was signed, its kind, its number."""
    purpose = row.purpose or SignedOriginal.PURPOSE_ORIGINAL
    year = timezone.localtime(row.signed_at).year if row.signed_at else 'unsigned'
    number = (row.number or str(row.pk)).replace('/', '-')
    return f'{purpose}/{year}/{row.kind}/{number}.pdf'


def _multipart(metadata: dict, pdf: bytes) -> tuple[bytes, str]:
    boundary = f'kogo-{uuid.uuid4().hex}'
    head = (
        f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'
        f'{json.dumps(metadata, ensure_ascii=False)}\r\n'
        f'--{boundary}\r\nContent-Type: application/pdf\r\n\r\n'
    ).encode('utf-8')
    return head + pdf + f'\r\n--{boundary}--\r\n'.encode('ascii'), boundary


def upload(row: SignedOriginal) -> bool:
    """
    Copy one signed row to the bucket. True when the copy is there (now, or from before).

    SigningUnavailable when the bucket cannot be reached with our credentials
    (the caller stops: every further row would fail the same way); any other
    refusal raises RuntimeError with Google's status, never a token.
    """
    from apps.documents.signing.kms import _google_error, access_token

    name = bucket()
    if not name:
        raise SigningUnavailable('SIGNING_BACKUP_BUCKET is not set')
    pdf = bytes(row.pdf or b'')
    if not row.signed_at or not pdf:
        raise RuntimeError('The row has no signed file')
    if hashlib.sha256(pdf).hexdigest() != row.sha256:
        # The database copy no longer matches its own record: never spread it.
        raise RuntimeError('The stored bytes do not match their SHA-256')

    metadata = {
        'name': object_name(row),
        'contentType': 'application/pdf',
        'md5Hash': base64.b64encode(hashlib.md5(pdf).digest()).decode('ascii'),
        'metadata': {
            'number': row.number,
            'kind': row.kind,
            'purpose': row.purpose or SignedOriginal.PURPOSE_ORIGINAL,
            'sha256': row.sha256,
            'signed_at': row.signed_at.isoformat(),
            'cert_fingerprint': row.cert_fingerprint,
            'key_id': row.key_id,
        },
    }
    body, boundary = _multipart(metadata, pdf)
    try:
        response = requests.post(
            UPLOAD_URL.format(bucket=name),
            params={'uploadType': 'multipart', 'ifGenerationMatch': '0'},
            headers={
                'Authorization': f'Bearer {access_token()}',
                'Content-Type': f'multipart/related; boundary={boundary}',
            },
            data=body,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SigningUnavailable(f'backup upload: {type(exc).__name__}') from exc
    if response.status_code in (200, 201):
        return True
    if response.status_code == 412:
        # The name is taken: the copy was written before (only this code writes
        # there, and the retention policy keeps anything from replacing it).
        return True
    detail = _google_error(response)
    if response.status_code in (401, 403, 404):
        raise SigningUnavailable(f'backup upload refused ({detail})')
    raise RuntimeError(f'backup upload refused ({detail})')


def backup_pending(*, limit: int = DEFAULT_LIMIT, time_budget_seconds: float = DEFAULT_TIME_BUDGET) -> dict:
    """
    Copy signed rows not yet in the bucket, oldest signature first. {copied, failed, remaining, stopped}.

    Does nothing (disabled) while SIGNING_BACKUP_BUCKET is empty. One row's
    failure is recorded on it and the run goes on; the bucket out of reach
    stops the run (`stopped`).
    """
    if not bucket():
        return {'disabled': True}
    started = time.monotonic()
    summary = {'copied': 0, 'failed': 0, 'remaining': 0, 'stopped': ''}
    due = (
        SignedOriginal.objects
        .filter(signed_at__isnull=False, backup_at__isnull=True)
        .order_by('signed_at')
    )
    for row in due[:max(1, int(limit))]:
        if summary['copied'] + summary['failed'] and time.monotonic() - started >= time_budget_seconds:
            break
        try:
            upload(row)
        except SigningUnavailable as exc:
            summary['stopped'] = str(exc)[:300]
            SignedOriginal.objects.filter(pk=row.pk).update(backup_error=summary['stopped'])
            logger.warning('Signing backup: stopped at %s — %s', row.number, summary['stopped'])
            break
        except Exception as exc:
            summary['failed'] += 1
            SignedOriginal.objects.filter(pk=row.pk).update(backup_error=f'{type(exc).__name__}: {exc}'[:300])
            logger.exception('Signing backup: %s could not be copied', row.number)
            continue
        SignedOriginal.objects.filter(pk=row.pk).update(backup_at=timezone.now(), backup_error='')
        summary['copied'] += 1
    summary['remaining'] = due.count()
    if summary['copied'] or summary['failed'] or summary['stopped']:
        logger.info('Signing backup: %s copied, %s failed, %s remaining%s', summary['copied'],
                    summary['failed'], summary['remaining'],
                    f" (stopped: {summary['stopped']})" if summary['stopped'] else '')
    return summary
