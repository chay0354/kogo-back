"""
WhatsApp to municipality parents — a phone and a name, nothing else.

The customers-list broadcast is built around a Child, a Family and a parent
phone, and none of those exist here. What does exist is
``ManyChatService.send_automation_to_contact``, which takes a phone and a name
and creates the recipient if ManyChat has never seen them. That is the whole
mechanism; this module is the preview-then-send wrapper around it.

Only free-form flows go out. The Kogo registration templates carry a course,
day and time from a paying registration, and sending one to a municipality
parent would arrive full of dashes.

Nothing here sends unless ``dry_run`` is False; the default is a preview.
"""
from __future__ import annotations

import logging
from typing import Iterable

from apps.core.manychat_service import ManyChatError, ManyChatService

logger = logging.getLogger(__name__)

# Same ceiling as the customers broadcast: every real send costs a few ManyChat
# calls, and the request has to answer before Vercel gives up. The client chunks.
BROADCAST_MAX_STUDENTS = 25


def broadcast_to_external_students(
    students: Iterable,
    *,
    automation_id: str,
    dry_run: bool = True,
    skip_phones: Iterable[str] = (),
    service: ManyChatService | None = None,
) -> dict:
    """
    Send (or preview) one ManyChat flow to ``students`` who have a phone.

    Returns a row per student with sent / failed / preview / skipped, and the
    E.164 phones used, so the caller can hand them back as ``skip_phones`` for
    the next chunk and two siblings on one phone still get a single message.
    """
    svc = service or ManyChatService()
    seen_phones: set[str] = {ManyChatService.normalize_phone_e164(p) for p in skip_phones if p}
    seen_phones.discard('')

    results: list[dict] = []
    counts = {'sent': 0, 'failed': 0, 'skipped': 0, 'preview': 0}
    phones_used: list[str] = []

    for student in students:
        row = {
            'student_id': str(student.id),
            'student_name': student.full_name,
            'phone': '',
            'status': 'skipped',
            'reason': None,
            'method': None,
            'error': None,
        }

        phone_key = ManyChatService.normalize_phone_e164(student.phone or '')
        row['phone'] = phone_key or (student.phone or '')
        if not phone_key:
            row['reason'] = 'no_phone'
            counts['skipped'] += 1
            results.append(row)
            continue
        if phone_key in seen_phones:
            row['reason'] = 'duplicate_phone'
            counts['skipped'] += 1
            results.append(row)
            continue

        if dry_run:
            seen_phones.add(phone_key)
            phones_used.append(phone_key)
            row['status'] = 'preview'
            counts['preview'] += 1
            results.append(row)
            continue

        branch = getattr(getattr(student.lesson, 'course', None), 'branch', None)
        try:
            outcome = svc.send_automation_to_contact(
                automation_type='flow',
                automation_id=automation_id,
                phone=student.phone,
                name=student.full_name,
                branch_name=getattr(branch, 'name', None),
            )
        except ManyChatError as exc:
            outcome = {'sent': False, 'error': str(exc)}

        if outcome.get('sent'):
            # Only a message that actually went out covers the second student on
            # the same phone; after a failure that row is still worth a try.
            seen_phones.add(phone_key)
            phones_used.append(phone_key)
            row['status'] = 'sent'
            row['method'] = outcome.get('method')
            counts['sent'] += 1
        else:
            row['status'] = 'failed'
            row['error'] = outcome.get('error') or outcome.get('reason') or 'unknown'
            counts['failed'] += 1
            logger.warning('External broadcast to %s failed: %s', student.id, row['error'])
        results.append(row)

    return {
        'dry_run': dry_run,
        'automation_type': 'flow',
        'automation_id': automation_id,
        'total': len(results),
        'sent': counts['sent'],
        'failed': counts['failed'],
        'skipped': counts['skipped'],
        'preview_count': counts['preview'],
        'phones': phones_used,
        'results': results,
    }
