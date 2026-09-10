"""
Credit a paid trial lesson against the first charge of a real registration.

A parent who paid, say, ₪30 for a trial and then signs the child up should not
pay that ₪30 again: it comes off the first charge, once, and the widget says so
in words. The credit is a property of the charge that used it — `Payment
.trial_credit_amount` with `trial_credit_source` pointing at the trial payment —
so "was this trial already credited?" is a question about rows, not a flag that
can drift.

Owner's rules (2026-09-09): any course in the same branch as the trial, within
60 days of the trial lesson, capped at the first charge (nothing is carried over
or refunded), on every registration path.
"""
import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.customers.models import Child, Payment

logger = logging.getLogger(__name__)

TRIAL_CREDIT_WINDOW_DAYS = 60
# A pending charge holds its credit only briefly: an abandoned checkout must not
# lock the parent's money away. Matches the sibling-discount window.
PENDING_HOLD = timedelta(hours=2)


def money(value) -> Decimal:
    return Decimal(str(value or '0')).quantize(Decimal('0.01'))


def shekels(value: Decimal) -> str:
    """₪50, not ₪50.00 — agorot only when there are any."""
    whole = value.quantize(Decimal('1')) if value == value.to_integral_value() else value
    return f'{whole:f}'


MIN_ID_DIGITS = 5


def _candidate_children(child):
    """
    The cards that are this child for money purposes: every card in the family,
    plus a card in another family that carries the same id number **and** the
    same parent — the widget opens a second family whenever a parent types their
    id differently, and that split must not cost them their credit.

    A bare id-number match across unrelated families is deliberately not enough:
    the repeat-trial guard may refuse a lesson on a weak match, but money may not
    move on one.
    """
    from apps.enrollments.repeat_trial import id_number_variants, normalize_id_number

    family = child.family
    cards = list(Child.objects.filter(family_id=child.family_id)) if child.family_id else [child]
    if not any(card.id == child.id for card in cards):
        cards.append(child)

    digits = normalize_id_number(getattr(child, 'id_number', '') or '')
    if len(digits.lstrip('0')) < MIN_ID_DIGITS or family is None:
        return cards

    parent_id = normalize_id_number(getattr(family, 'parent_id_number', '') or '')
    phone = (family.phone or '').strip()
    if not parent_id and not phone:
        return cards

    twin_filter = Q()
    if parent_id:
        twin_filter |= Q(family__parent_id_number__in=id_number_variants(family.parent_id_number or ''))
    if phone:
        twin_filter |= Q(family__phone=phone)
    twins = (
        Child.objects
        .filter(id_number__in=id_number_variants(child.id_number or ''))
        .filter(twin_filter)
        .exclude(family_id=child.family_id)
        .select_related('family')
    )
    cards.extend(twins)
    return cards


def _is_spent(trial: Payment, *, now=None, ignore_payment_ids=()) -> bool:
    """
    True once a registration charge has taken this trial's credit.

    `ignore_payment_ids` are charges this quote replaces — the pending row a CRM
    price preview left behind, or the parent's own earlier attempt at the same
    lesson. Without that, quoting a price twice would starve the real charge.
    """
    now = now or timezone.now()
    uses = trial.trial_credit_uses.filter(trial_credit_amount__gt=0)
    if ignore_payment_ids:
        uses = uses.exclude(id__in=list(ignore_payment_ids))
    if uses.filter(status__in=('completed', 'processing')).exists():
        return True
    return uses.filter(status='pending', created_at__gte=now - PENDING_HOLD).exists()


def superseded_pending_ids(child, lesson) -> list:
    """
    Pending signup charges for this exact child and lesson: a price preview, or an
    attempt the parent abandoned and is now repeating. A new charge for the same
    thing replaces them, so they must not count as having spent the credit.
    """
    if child is None or lesson is None:
        return []
    return list(
        Payment.objects
        .filter(
            child__in=_candidate_children(child),
            lesson=lesson,
            payment_type='recurring_subscription',
            status='pending',
            trial_credit_amount__gt=0,
        )
        .values_list('id', flat=True)
    )


def credit_still_held_by(payment: Payment) -> bool:
    """
    Re-check, at charge time, that this row may still spend the trial it was
    promised. A checkout left open for hours can come back after the credit went
    to another registration; charging it then would give the same trial twice.
    """
    trial = payment.trial_credit_source
    if trial is None or (payment.trial_credit_amount or Decimal('0')) <= 0:
        return True
    taken = (
        trial.trial_credit_uses
        .filter(trial_credit_amount__gt=0, status__in=('completed', 'processing'))
        .exclude(id=payment.id)
        .exists()
    )
    return not taken


def creditable_trial_payment(child, *, branch_id, today: date | None = None, now=None, ignore_payment_ids=()) -> Payment | None:
    """
    The paid trial this child may still cash in for a registration in `branch_id`:
    charged, inside the window, and not already credited. The oldest one first, so
    a parent who paid for two trials loses neither by the order they register.
    """
    if child is None or branch_id is None:
        return None
    today = today or timezone.localdate()
    earliest = today - timedelta(days=TRIAL_CREDIT_WINDOW_DAYS)
    # A trial already paid for but still ahead counts too: the money left the
    # parent's card, which is what the credit is about.
    latest = today + timedelta(days=TRIAL_CREDIT_WINDOW_DAYS)
    rows = (
        Payment.objects
        .filter(
            child__in=_candidate_children(child),
            payment_type='one_time',
            status='completed',
            trial_lesson_date__isnull=False,
            trial_lesson_date__gte=earliest,
            trial_lesson_date__lte=latest,
            final_amount__gt=0,
        )
        .filter(Q(branch_id=branch_id) | Q(lesson__course__branch_id=branch_id))
        .select_related('lesson', 'lesson__course')
        .order_by('trial_lesson_date', 'created_at')
    )
    for trial in rows:
        if not _is_spent(trial, now=now, ignore_payment_ids=ignore_payment_ids):
            return trial
    return None


def quote_trial_credit(child, *, branch_id, first_charge: Decimal, today: date | None = None, ignore_payment_ids=()) -> dict:
    """
    What to take off this first charge, and the sentence the parent reads.

    `amount` is capped at the charge itself: a trial worth more than the first
    month simply zeroes it, with no leftover (the owner's rule).
    """
    empty = {'amount': Decimal('0.00'), 'source': None, 'trial_paid': Decimal('0.00'), 'trial_date': None, 'reason': ''}
    today = today or timezone.localdate()
    charge = money(first_charge)
    if charge <= 0:
        return empty
    trial = creditable_trial_payment(child, branch_id=branch_id, today=today, ignore_payment_ids=ignore_payment_ids)
    if trial is None:
        return empty
    paid = money(trial.final_amount)
    amount = min(paid, charge)
    if amount <= 0:
        return empty
    when = trial.trial_lesson_date.strftime('%d/%m/%Y') if trial.trial_lesson_date else ''
    ahead = bool(trial.trial_lesson_date and today and trial.trial_lesson_date > today)
    opening = (
        f'שילמתם ₪{shekels(paid)} על שיעור ניסיון שנקבע ל־{when}' if ahead
        else f'שמנו לב שכבר הייתם אצלנו בשיעור ניסיון ב־{when} ושילמתם עליו ₪{shekels(paid)}'
    )
    reason = f'{opening} — הסכום מקוזז מהתשלום הראשון.'
    if amount < paid:
        reason = f'{opening} — מקוזזים ₪{shekels(amount)}, עד גובה התשלום הראשון.'
    return {'amount': amount, 'source': trial, 'trial_paid': paid, 'trial_date': trial.trial_lesson_date, 'reason': reason}


def credit_for_lesson(child, lesson, *, first_charge: Decimal, today: date | None = None) -> dict:
    """
    `quote_trial_credit` for a registration to `lesson` — the branch comes from
    its course, and a pending charge for this same lesson (a CRM price preview,
    or an attempt the parent is repeating) does not count against the quote.
    """
    branch_id = lesson.course.branch_id if lesson is not None and lesson.course_id else None
    return quote_trial_credit(
        child, branch_id=branch_id, first_charge=first_charge, today=today,
        ignore_payment_ids=superseded_pending_ids(child, lesson),
    )


def describe(quote: dict) -> dict:
    """The credit as it travels to a client (widget, CRM dialog, card-link page)."""
    return {
        'trial_credit_amount': float(quote['amount']),
        'trial_credit_paid': float(quote['trial_paid']),
        'trial_credit_date': quote['trial_date'].isoformat() if quote.get('trial_date') else None,
        'trial_credit_reason': quote['reason'],
    }
