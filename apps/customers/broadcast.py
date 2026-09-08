"""WhatsApp broadcast from the customers list — one ManyChat automation to many children.

The office filters the list, selects the children, and sends. Unlike the
old WhatsApp page (contacts only), each row here knows its child and lesson,
so the Kogo templates (kind) go out with the child's own course, day and time.

Nothing here sends unless ``dry_run`` is False; the default is a preview.
"""
from __future__ import annotations

import logging
from typing import Iterable

from apps.core.enrollment_whatsapp import build_enrollment_whatsapp_context
from apps.core.manychat_service import ManyChatError, ManyChatService

logger = logging.getLogger(__name__)

# One request handles at most this many children — every real send costs a
# few ManyChat calls plus a settle sleep, and the function has to answer
# before Vercel's timeout. The client sends smaller chunks than this.
BROADCAST_MAX_CHILDREN = 25


def _active_lesson_for(child):
    """The lesson whose details go into the message: the first active enrollment."""
    rows = [
        row for row in child.lesson_enrollments.all()
        if row.status == 'active' and row.lesson_id
    ]
    if not rows:
        return None
    rows.sort(key=lambda row: (row.start_date or row.created_at.date()))
    return rows[0].lesson


def broadcast_to_children(
    children: Iterable,
    *,
    automation_type: str,
    automation_id: str,
    dry_run: bool = True,
    skip_phones: Iterable[str] = (),
    service: ManyChatService | None = None,
) -> dict:
    """
    Send (or preview) one automation to the parents of ``children``.

    Returns per-child rows with status sent / failed / preview / skipped and
    the E.164 phones used, so the caller can hand them back as ``skip_phones``
    for the next chunk and siblings across chunks still get one message.
    """
    svc = service or ManyChatService()
    seen_phones: set[str] = {ManyChatService.normalize_phone_e164(p) for p in skip_phones if p}
    seen_phones.discard('')

    results: list[dict] = []
    counts = {'sent': 0, 'failed': 0, 'skipped': 0, 'preview': 0}
    phones_used: list[str] = []

    for child in children:
        row = {
            'child_id': str(child.id),
            'child_name': f'{child.first_name} {child.last_name}'.strip(),
            'parent_name': '',
            'phone': '',
            'status': 'skipped',
            'reason': None,
            'method': None,
            'error': None,
        }
        lesson = _active_lesson_for(child)
        ctx = build_enrollment_whatsapp_context(child=child, lesson=lesson)
        if not ctx:
            row['reason'] = 'no_parent_phone'
            counts['skipped'] += 1
            results.append(row)
            continue

        row['parent_name'] = ctx.get('parent_name') or ''
        phone_key = ManyChatService.normalize_phone_e164(ctx['phone'])
        row['phone'] = phone_key or ctx['phone']
        if not phone_key:
            row['reason'] = 'no_parent_phone'
            counts['skipped'] += 1
            results.append(row)
            continue
        if phone_key in seen_phones:
            row['reason'] = 'duplicate_phone'
            counts['skipped'] += 1
            results.append(row)
            continue
        if automation_type == 'kind' and lesson is None:
            # The Kogo templates carry course/day/time; without a lesson the
            # parent would get a message full of dashes.
            row['reason'] = 'no_active_lesson'
            counts['skipped'] += 1
            results.append(row)
            continue

        seen_phones.add(phone_key)
        phones_used.append(phone_key)

        if dry_run:
            row['status'] = 'preview'
            counts['preview'] += 1
            results.append(row)
            continue

        try:
            if automation_type == 'kind':
                outcome = svc.notify_registration(
                    kind=automation_id,
                    phone=ctx['phone'],
                    parent_name=ctx['parent_name'],
                    child_name=ctx['child_name'],
                    course_name=ctx['course_name'],
                    day_name=ctx['day_name'],
                    start_time=ctx['start_time'],
                    end_time=ctx['end_time'],
                    branch_name=ctx['branch_name'],
                    location=ctx.get('location', ''),
                    lookup_names=ctx.get('lookup_names'),
                )
            else:
                outcome = svc.send_automation_to_contact(
                    automation_type='flow',
                    automation_id=automation_id,
                    phone=ctx['phone'],
                    name=ctx['parent_name'],
                    branch_name=ctx.get('branch_name') or None,
                )
        except ManyChatError as exc:
            outcome = {'sent': False, 'error': str(exc)}

        if outcome.get('sent'):
            row['status'] = 'sent'
            row['method'] = outcome.get('method')
            counts['sent'] += 1
        else:
            row['status'] = 'failed'
            row['error'] = outcome.get('error') or outcome.get('reason') or 'unknown'
            counts['failed'] += 1
            logger.warning('Broadcast to child %s failed: %s', child.id, row['error'])
        results.append(row)

    return {
        'dry_run': dry_run,
        'automation_type': automation_type,
        'automation_id': automation_id,
        'total': len(results),
        'sent': counts['sent'],
        'failed': counts['failed'],
        'skipped': counts['skipped'],
        'preview_count': counts['preview'],
        'phones': phones_used,
        'results': results,
    }
