"""
Fix split twice/thrice-a-week standing orders to one amount = widget combined_price
(minus discounts once).

Whoever holds the schedule for that card gets corrected: when Tranzila has a
standing order its amount is replaced there and duplicates are inactivated;
otherwise the CRM cron is the biller and only its amount changes. Extra CRM rows
are cancelled either way.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.db import transaction
from django.utils import timezone

from apps.core.tranzila_service import TranzilaService
from apps.courses.models import LessonBundle
from apps.customers.models import RecurringPayment
from apps.customers.recurring_amount import schedule_recurring_amount
from apps.enrollments.models import LessonEnrollment

Q2 = Decimal('0.01')
SKIP_NAME_FRAGMENTS = ('ניסיון', 'test', 'טסט')
CANCEL_REASON = 'סנכרון מסלול משולב — הוראת קבע כפולה'
# Batch size per request. Every child costs a Tranzila round trip, so a bigger
# batch overruns the request timeout; the caller repeats until `remaining` is 0.
DEFAULT_LIMIT = 5


def _d(value) -> Decimal:
    if value is None:
        return Decimal('0.00')
    return Decimal(str(value)).quantize(Q2, rounding=ROUND_HALF_UP)


def _close(a, b, tol: Decimal = Decimal('1.00')) -> bool:
    return abs(_d(a) - _d(b)) <= tol


def _is_test_child(child) -> bool:
    name = f'{child.first_name} {child.last_name}'.lower()
    return any(fragment.lower() in name for fragment in SKIP_NAME_FRAGMENTS)


def expected_bundle_amount(combined: Decimal, stos: list[RecurringPayment]) -> Decimal:
    combined = _d(combined)
    details = None
    for rp in stos:
        raw = rp.discount_details
        if isinstance(raw, list) and raw:
            details = raw
            break
        if isinstance(raw, dict) and raw:
            details = [raw]
            break
    if not details:
        discounts = [_d(rp.discount_amount) for rp in stos if _d(rp.discount_amount) > 0]
        if not discounts:
            return combined
        return (combined - max(discounts)).quantize(Q2)

    deducted = Decimal('0.00')
    seen = set()
    for item in details:
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get('name') or ''),
            str(item.get('type') or ''),
            str(item.get('value') or ''),
        )
        if key in seen:
            continue
        seen.add(key)
        dtype = str(item.get('type') or '').lower()
        try:
            value = Decimal(str(item.get('value') or '0'))
        except Exception:
            value = Decimal('0')
        try:
            snap = Decimal(str(item.get('amount_deducted') or '0'))
        except Exception:
            snap = Decimal('0')
        if dtype in ('percentage', 'percent', 'אחוז'):
            deducted += (combined * value / Decimal('100')).quantize(Q2)
        elif snap > 0:
            deducted += snap
        else:
            deducted += value
    return (combined - deducted).quantize(Q2)


@dataclass
class BundleStoFix:
    child_id: str
    child_name: str
    phone: str
    course_name: str
    keep: RecurringPayment
    extras: list[RecurringPayment]
    expected: Decimal
    earliest: datetime


def _looks_like_split(rp: RecurringPayment, split: Decimal) -> bool:
    amount = _d(rp.amount)
    base = _d(rp.base_amount) if rp.base_amount is not None else amount
    discount = _d(rp.discount_amount)
    return (
        _close(amount, split, Decimal('1.50'))
        or _close(base, split, Decimal('1.50'))
        or _close(amount + discount, split, Decimal('1.50'))
    )


def iter_bundle_sto_fixes() -> list[BundleStoFix]:
    bundles = list(
        LessonBundle.objects.select_related('course').prefetch_related('lessons')
    )
    bundle_by_id = {str(bundle.id): bundle for bundle in bundles}
    lessons_by_bundle = {
        str(bundle.id): list(bundle.lessons.all()) for bundle in bundles
    }
    bundles_for_lesson: dict[str, list[LessonBundle]] = {}
    for bundle in bundles:
        for lesson in lessons_by_bundle[str(bundle.id)]:
            bundles_for_lesson.setdefault(str(lesson.id), []).append(bundle)

    stos = list(
        RecurringPayment.objects.filter(status='active')
        .select_related(
            'child__family',
            'initial_payment__lesson__course',
            'initial_payment__bundle',
        )
        .order_by('created_at', 'id')
    )
    enrollments = list(
        LessonEnrollment.objects.filter(status='active').select_related('bundle', 'lesson')
    )
    enroll_by_child: dict[str, list[LessonEnrollment]] = {}
    for enrollment in enrollments:
        enroll_by_child.setdefault(str(enrollment.child_id), []).append(enrollment)

    by_child: dict[str, list[RecurringPayment]] = {}
    for rp in stos:
        if rp.child_id and not _is_test_child(rp.child):
            by_child.setdefault(str(rp.child_id), []).append(rp)

    fixes: list[BundleStoFix] = []
    for child_id, rp_list in by_child.items():
        bundle = _resolve_bundle(
            rp_list,
            enroll_by_child.get(child_id, []),
            bundle_by_id,
            bundles_for_lesson,
            lessons_by_bundle,
        )
        if bundle is None:
            continue
        members = lessons_by_bundle[str(bundle.id)]
        if len(members) < 2:
            continue
        member_ids = {str(lesson.id) for lesson in members}
        split = (_d(bundle.combined_price) / len(members)).quantize(Q2)
        single_price = _d(bundle.course.price)

        bundle_stos = [
            rp for rp in rp_list
            if _sto_belongs_to_bundle(rp, bundle, member_ids)
        ] or list(rp_list)

        if _is_once_a_week_choice(bundle_stos, enroll_by_child.get(child_id, []), member_ids, single_price):
            continue

        expected = expected_bundle_amount(bundle.combined_price, bundle_stos)
        ordered = sorted(bundle_stos, key=lambda rp: (rp.created_at, str(rp.id)))
        keep = ordered[0]
        extras = ordered[1:]
        pending_matches = (
            keep.pending_amount is not None
            and _close(keep.pending_amount, expected, Decimal('0.00'))
        )
        amount_matches = _close(keep.amount, expected, Decimal('0.00'))
        # A raise already waiting for the next cycle means this child was handled
        # (here or by a manager), so leave the scheduled amount alone even if it
        # no longer matches what we would compute today.
        raise_already_scheduled = (
            keep.pending_amount is not None
            and _d(keep.pending_amount) > _d(keep.amount)
        )
        if not extras and (amount_matches or pending_matches or raise_already_scheduled):
            continue

        child = keep.child
        phone = child.family.phone if child.family else (child.phone_number or '')
        course_name = bundle.course.name
        if keep.initial_payment and keep.initial_payment.lesson:
            course_name = keep.initial_payment.lesson.course.name
        fixes.append(
            BundleStoFix(
                child_id=child_id,
                child_name=f'{child.first_name} {child.last_name}'.strip(),
                phone=phone,
                course_name=course_name,
                keep=keep,
                extras=extras,
                expected=expected,
                earliest=keep.created_at,
            )
        )

    fixes.sort(key=lambda item: (item.earliest, item.child_name))
    return fixes


def _sto_belongs_to_bundle(rp: RecurringPayment, bundle: LessonBundle, member_ids: set[str]) -> bool:
    payment = rp.initial_payment
    if payment is None:
        return False
    if payment.bundle_id and str(payment.bundle_id) == str(bundle.id):
        return True
    return bool(payment.lesson_id and str(payment.lesson_id) in member_ids)


def _resolve_bundle(
    rp_list: list[RecurringPayment],
    enrollments: list[LessonEnrollment],
    bundle_by_id: dict[str, LessonBundle],
    bundles_for_lesson: dict[str, list[LessonBundle]],
    lessons_by_bundle: dict[str, list],
) -> Optional[LessonBundle]:
    for rp in rp_list:
        payment = rp.initial_payment
        if payment and payment.bundle_id:
            bundle = bundle_by_id.get(str(payment.bundle_id))
            if bundle and len(lessons_by_bundle.get(str(bundle.id), [])) >= 2:
                return bundle
    for enrollment in enrollments:
        if enrollment.bundle_id:
            bundle = bundle_by_id.get(str(enrollment.bundle_id))
            if bundle and len(lessons_by_bundle.get(str(bundle.id), [])) >= 2:
                return bundle
    for rp in rp_list:
        payment = rp.initial_payment
        lesson = payment.lesson if payment else None
        if lesson is None:
            continue
        for bundle in bundles_for_lesson.get(str(lesson.id), []):
            members = lessons_by_bundle[str(bundle.id)]
            if len(members) < 2:
                continue
            split = (_d(bundle.combined_price) / len(members)).quantize(Q2)
            if _looks_like_split(rp, split):
                return bundle
    return None


def _is_once_a_week_choice(
    bundle_stos: list[RecurringPayment],
    enrollments: list[LessonEnrollment],
    member_ids: set[str],
    single_price: Decimal,
) -> bool:
    if len(bundle_stos) != 1:
        return False
    rp = bundle_stos[0]
    payment = rp.initial_payment
    if payment and payment.bundle_id:
        return False
    if any(e.bundle_id and str(e.lesson_id) in member_ids for e in enrollments):
        return False
    return _close(_d(rp.amount), single_price, Decimal('1.00'))


def _cancel_locally(rp: RecurringPayment, reason: str) -> None:
    rp.status = 'cancelled'
    rp.cancelled_at = timezone.now()
    rp.cancellation_reason = reason
    rp.save(update_fields=['status', 'cancelled_at', 'cancellation_reason'])


def apply_bundle_sto_fixes(limit: int = DEFAULT_LIMIT) -> dict:
    if limit < 1:
        limit = DEFAULT_LIMIT
    pending = iter_bundle_sto_fixes()
    chosen = pending[:limit]
    synced = []
    failed = []
    tranzila = TranzilaService.production()
    for fix in chosen:
        keep = RecurringPayment.objects.get(pk=fix.keep.pk)
        token = (keep.tranzila_token or '').strip()
        gateway = tranzila.sync_standing_order_to_amount(
            token=token,
            amount=fix.expected,
            item_name=fix.course_name,
        )
        if not gateway.get('success'):
            failed.append({
                'child': fix.child_name,
                'phone': fix.phone,
                'course': fix.course_name,
                'error': gateway.get('error') or gateway.get('message') or 'שגיאה בטרנזילה',
            })
            continue

        with transaction.atomic():
            keep = RecurringPayment.objects.select_for_update().get(pk=fix.keep.pk)
            extras = list(
                RecurringPayment.objects.select_for_update().filter(
                    pk__in=[extra.pk for extra in fix.extras],
                    status='active',
                )
            )
            cancelled_ids = []
            for extra in extras:
                _cancel_locally(extra, CANCEL_REASON)
                cancelled_ids.append(str(extra.id))
            old_amount = _d(keep.amount)
            updated = keep
            if not _close(old_amount, fix.expected, Decimal('0.00')):
                updated = schedule_recurring_amount(keep, fix.expected)
            sto_id = str(gateway.get('sto_id') or '')
            if sto_id:
                # Tranzila now owns the schedule, so the CRM cron must stand down.
                RecurringPayment.objects.filter(pk=updated.pk).update(
                    tranzila_recurring_index=sto_id,
                )
            updated.refresh_from_db()
            synced.append({
                'child': fix.child_name,
                'phone': fix.phone,
                'course': fix.course_name,
                'kept_id': str(updated.id),
                'old_amount': str(old_amount),
                'new_amount': str(fix.expected),
                'tranzila_sto_id': sto_id,
                'tranzila_action': gateway.get('action'),
                'pending_amount': str(updated.pending_amount) if updated.pending_amount is not None else None,
                'pending_from': (
                    updated.pending_amount_effective_date.isoformat()
                    if updated.pending_amount_effective_date else None
                ),
                'cancelled_ids': cancelled_ids,
            })
    remaining = max(0, len(pending) - len(synced))
    return {
        'synced': synced,
        'failed': failed,
        'synced_count': len(synced),
        'failed_count': len(failed),
        'remaining': remaining,
    }
