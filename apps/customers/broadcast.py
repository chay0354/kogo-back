"""WhatsApp broadcast from the customers list — one ManyChat automation to many children.

The office filters the list, selects the children, and sends. Unlike the
old WhatsApp page (contacts only), each row here knows its child and lesson,
so the Kogo templates (kind) go out with the child's own course, day and time.

Nothing here sends unless ``dry_run`` is False; the default is a preview.

Whoever asked "הסר" gets no marketing broadcast (apps/core/whatsapp_optout.py).
The list is read once, before the first message of the request, and a phone on
it is skipped in whatever role it appears — the parent the message is for, or
one of the family's extra phones. The other parent, on a number of their own,
did not ask and still gets it. When the list cannot be read nothing is sent at
all, the preview included: `OptOutListUnavailable` goes up to the caller.

The office says, for every broadcast, whether it is marketing (15.10.2026). "הסר"
is a request not to get marketing: an update about the child's own class — a
lesson moved, the studio closed — is service, and goes to them too. Marketing
is what a request that does not say is taken to be, so a screen that never
asks keeps skipping them. A message that is not marketing still reads the list,
to say how many who asked out got it (`included_opted_out`).
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from apps.core import whatsapp_optout
from apps.core.enrollment_whatsapp import build_enrollment_whatsapp_context
from apps.core.manychat_service import (
    ManyChatContactUnfindable,
    ManyChatError,
    ManyChatService,
    manychat_error_detail,
)
from apps.core.scoping import ACTIVE_ENROLLMENT_STATUSES

logger = logging.getLogger(__name__)

# One request handles at most this many children — every real send costs a
# few ManyChat calls plus a settle sleep, and the function has to answer
# before Vercel's timeout. The client sends smaller chunks than this.
BROADCAST_MAX_CHILDREN = 25


def _active_lesson_for(child, *, lesson_id=None, day_of_week=None):
    """
    The lesson whose details go into the message.

    The office built the audience with a filter; when the request says which
    lesson or weekday that was, a child in several slots gets that one. Failing
    a match, the earliest-started active enrollment. 'payments_problem' counts
    as active here as everywhere else in the CRM — a "payment failed" template
    is for exactly those children.
    """
    rows = [
        row for row in child.lesson_enrollments.all()
        if row.status in ACTIVE_ENROLLMENT_STATUSES and row.lesson_id
    ]
    if not rows:
        return None
    if lesson_id:
        for row in rows:
            if str(row.lesson_id) == str(lesson_id):
                return row.lesson
    if day_of_week is not None:
        matching = [row for row in rows if row.lesson.day_of_week == day_of_week]
        if matching:
            rows = matching
    rows.sort(key=lambda row: (row.start_date or row.created_at.date()))
    return rows[0].lesson


_MOBILE_E164 = re.compile(r'^9725\d{8}$')


def _extra_recipients(child, phone_key: str) -> list:
    """
    The family's other parents with a phone of their own — the extra phones the
    office added on the card, which get the group message too — each with the
    reason it is skipped, or None.

    Every parent but the one the message already went to, the same split the
    card shows (customer_details.extra_phones_of). A number equal to the
    primary's is the same person and is left out quietly. A landline is listed
    but skipped: the "add customer" form has always taken one as the extra
    phone, and WhatsApp does not reach it.
    """
    extras = []
    for parent in child.family.parents.all():
        key = ManyChatService.normalize_phone_e164(parent.phone or '')
        if not key or key == phone_key:
            continue
        extras.append((parent, key, None if _MOBILE_E164.match(key) else 'not_mobile'))
    return extras


def _extra_lookup_names(parent, family) -> list[str]:
    names: list[str] = []
    for value in (f'{parent.first_name} {parent.last_name}', parent.first_name, parent.last_name, family.name):
        value = (value or '').strip()
        if value and value not in names:
            names.append(value)
    return names


def broadcast_to_children(
    children: Iterable,
    *,
    automation_type: str,
    automation_id: str,
    dry_run: bool = True,
    skip_phones: Iterable[str] = (),
    service: ManyChatService | None = None,
    lesson_id=None,
    day_of_week=None,
    include_extra_phones: bool = False,
    marketing: bool = True,
) -> dict:
    """
    Send (or preview) one automation to the parents of ``children``.

    Returns per-child rows with status sent / failed / preview / skipped and
    the E.164 phones used, so the caller can hand them back as ``skip_phones``
    for the next chunk and siblings across chunks still get one message.

    The row's own status is the primary parent's. With include_extra_phones
    the family's extra phones go in the row's ``extra_phones``, each with its
    own status, under the same rules: one message per phone across the whole
    run, and none when the template has no lesson to fill in. Without it the
    list stays empty and nothing reaches them.

    Every parent in the request is sent to before any extra phone: a number
    that is one family's extra and another family's own phone gets the message
    about its own child, and the extra copy is then a duplicate.

    A phone that asked "הסר" is skipped with reason ``opted_out``, as the
    parent's phone or as an extra one, and is counted apart (``opted_out``,
    ``extra_opted_out``) inside the skipped — when the message is
    ``marketing``. When it is not, the phone is sent to like any other, and
    counted apart as one that got it although it asked out
    (``included_opted_out``, ``extra_included_opted_out``). Either way a row or
    an extra phone whose number is on the list says so (``opted_out: true``).
    Raises ``whatsapp_optout.OptOutListUnavailable`` — before anything is sent —
    when that list cannot be read.
    """
    svc = service or ManyChatService()
    # Read once, before the first message: no row of this request is sent unless the list was read.
    opted_out = whatsapp_optout.blocked_phones()
    seen_phones: set[str] = {ManyChatService.normalize_phone_e164(p) for p in skip_phones if p}
    seen_phones.discard('')

    results: list[dict] = []
    counts = {'sent': 0, 'failed': 0, 'skipped': 0, 'preview': 0, 'opted_out': 0, 'included_opted_out': 0}
    extra_counts = {'sent': 0, 'failed': 0, 'skipped': 0, 'preview': 0, 'opted_out': 0, 'included_opted_out': 0}

    def got_it_anyway(target: dict, tally: dict) -> None:
        """A message that is not marketing went (or, in the preview, would go) to a number that asked "הסר"."""
        if target['status'] in ('sent', 'preview'):
            tally['included_opted_out'] += 1
    phones_used: list[str] = []

    def deliver(*, phone: str, parent_name: str, lookup_names, ctx: dict) -> dict:
        try:
            if automation_type == 'kind':
                return svc.notify_registration(
                    kind=automation_id,
                    phone=phone,
                    parent_name=parent_name,
                    child_name=ctx['child_name'],
                    course_name=ctx['course_name'],
                    day_name=ctx['day_name'],
                    start_time=ctx['start_time'],
                    end_time=ctx['end_time'],
                    branch_name=ctx['branch_name'],
                    location=ctx.get('location', ''),
                    lookup_names=lookup_names,
                )
            return svc.send_automation_to_contact(
                automation_type='flow',
                automation_id=automation_id,
                phone=phone,
                name=parent_name,
                branch_name=ctx.get('branch_name') or None,
            )
        except ManyChatError as exc:
            # str(exc) is ManyChat's headline and is often just "Validation
            # error". What the office needs is the field it rejected, which
            # lives in the payload.
            outcome = {'sent': False, 'error': manychat_error_detail(exc)}
            if isinstance(exc, ManyChatContactUnfindable):
                outcome['reason'] = 'contact_unfindable'
            return outcome

    def settle(target: dict, key: str, outcome: dict, tally: dict) -> None:
        """Record one real send on its row; only a message that went out uses up the phone."""
        if outcome.get('sent'):
            # Only a message that went out covers the sibling on the same phone;
            # after a failure the sibling's row is still worth a try.
            seen_phones.add(key)
            phones_used.append(key)
            target['status'] = 'sent'
            target['method'] = outcome.get('method')
            tally['sent'] += 1
        else:
            target['status'] = 'failed'
            target['error'] = outcome.get('error') or outcome.get('reason') or 'unknown'
            if outcome.get('reason') == 'contact_unfindable':
                # The screen offers to link this contact by hand.
                target['reason'] = 'contact_unfindable'
            tally['failed'] += 1

    pending_extras: list[tuple[dict, object, dict, str]] = []

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
            'extra_phones': [],
        }
        results.append(row)
        lesson = _active_lesson_for(child, lesson_id=lesson_id, day_of_week=day_of_week)
        ctx = build_enrollment_whatsapp_context(child=child, lesson=lesson)
        if not ctx:
            row['reason'] = 'no_parent_phone'
            counts['skipped'] += 1
            continue

        row['parent_name'] = ctx.get('parent_name') or ''
        phone_key = ManyChatService.normalize_phone_e164(ctx['phone'])
        row['phone'] = phone_key or ctx['phone']
        if not phone_key:
            row['reason'] = 'no_parent_phone'
            counts['skipped'] += 1
            continue
        asked_out = phone_key in opted_out
        if asked_out:
            row['opted_out'] = True
        # Only marketing holds back whoever asked "הסר"; an update about their own class reaches them too.
        held_back = asked_out and marketing
        if not held_back and automation_type == 'kind' and lesson is None and phone_key not in seen_phones:
            # The Kogo templates carry course/day/time; without a lesson the
            # parent would get a message full of dashes — and so would the
            # extra phones, which are skipped with it.
            row['reason'] = 'no_active_lesson'
            counts['skipped'] += 1
            continue

        if held_back:
            # Asked "הסר": no marketing to this number, whatever else is true of the row. It does not
            # use the phone up, so it is said again on a sibling's row rather than "duplicate".
            row['reason'] = whatsapp_optout.REASON
            counts['skipped'] += 1
            counts['opted_out'] += 1
        elif phone_key in seen_phones:
            row['reason'] = 'duplicate_phone'
            counts['skipped'] += 1
        elif dry_run:
            seen_phones.add(phone_key)
            phones_used.append(phone_key)
            row['status'] = 'preview'
            counts['preview'] += 1
        else:
            outcome = deliver(
                phone=ctx['phone'],
                parent_name=ctx['parent_name'],
                lookup_names=ctx.get('lookup_names'),
                ctx=ctx,
            )
            settle(row, phone_key, outcome, counts)
            if row['status'] == 'failed':
                logger.warning('Broadcast to child %s failed: %s', child.id, row['error'])
        if asked_out and not marketing:
            got_it_anyway(row, counts)

        if include_extra_phones and not (automation_type == 'kind' and lesson is None):
            pending_extras.append((row, child, ctx, phone_key))

    for row, child, ctx, phone_key in pending_extras:
        for parent, key, skip_reason in _extra_recipients(child, phone_key):
            name = f'{parent.first_name} {parent.last_name}'.strip() or row['parent_name']
            extra = {
                'parent_name': name,
                'phone': key,
                'status': 'skipped',
                'reason': None,
                'method': None,
                'error': None,
            }
            row['extra_phones'].append(extra)
            if skip_reason:
                extra['reason'] = skip_reason
                extra_counts['skipped'] += 1
                continue
            extra_asked_out = key in opted_out
            if extra_asked_out:
                extra['opted_out'] = True
            if extra_asked_out and marketing:
                extra['reason'] = whatsapp_optout.REASON
                extra_counts['skipped'] += 1
                extra_counts['opted_out'] += 1
                continue
            if key in seen_phones:
                extra['reason'] = 'duplicate_phone'
                extra_counts['skipped'] += 1
                continue
            if dry_run:
                seen_phones.add(key)
                phones_used.append(key)
                extra['status'] = 'preview'
                extra_counts['preview'] += 1
            else:
                outcome = deliver(
                    phone=parent.phone,
                    parent_name=name,
                    lookup_names=_extra_lookup_names(parent, child.family),
                    ctx=ctx,
                )
                settle(extra, key, outcome, extra_counts)
                if extra['status'] == 'failed':
                    logger.warning('Broadcast to an extra phone of child %s failed: %s', child.id, extra['error'])
            if extra_asked_out:
                got_it_anyway(extra, extra_counts)

    # Which kind of message it was, beside what it did — the record of why somebody who asked "הסר" got it, or
    # did not. Counts only: no phone, no name.
    logger.info(
        'Customers broadcast %s:%s — %s message%s: %d rows, sent %d, preview %d, failed %d, skipped %d '
        '(asked "הסר" — skipped %d, included %d; extra phones — skipped %d, included %d)',
        automation_type, automation_id, 'marketing' if marketing else 'not marketing', ' (dry run)' if dry_run else '',
        len(results), counts['sent'], counts['preview'], counts['failed'], counts['skipped'],
        counts['opted_out'], counts['included_opted_out'], extra_counts['opted_out'], extra_counts['included_opted_out'],
    )

    return {
        'dry_run': dry_run,
        # Marketing skips whoever asked "הסר"; anything else reaches them too (the office chose, 15.10.2026).
        'marketing': marketing,
        'automation_type': automation_type,
        'automation_id': automation_id,
        'total': len(results),
        'sent': counts['sent'],
        'failed': counts['failed'],
        'skipped': counts['skipped'],
        'preview_count': counts['preview'],
        # How many of the skipped were skipped because they asked "הסר".
        'opted_out': counts['opted_out'],
        'extra_sent': extra_counts['sent'],
        'extra_failed': extra_counts['failed'],
        'extra_skipped': extra_counts['skipped'],
        'extra_preview_count': extra_counts['preview'],
        'extra_opted_out': extra_counts['opted_out'],
        # How many got it (or, in the preview, would) although they asked "הסר" — only a message that is not
        # marketing; 0 for marketing.
        'included_opted_out': counts['included_opted_out'],
        'extra_included_opted_out': extra_counts['included_opted_out'],
        'phones': phones_used,
        'results': results,
    }
