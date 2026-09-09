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

from apps.customers.models import Payment

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


def _candidate_children(child):
    """The child's own card and any duplicate card carrying the same id number."""
    from apps.enrollments.repeat_trial import _identity_twins

    return _identity_twins(child)


def _is_spent(trial: Payment, *, now=None) -> bool:
    """True once a registration charge has taken this trial's credit."""
    now = now or timezone.now()
    uses = trial.trial_credit_uses.filter(trial_credit_amount__gt=0)
    if uses.filter(status__in=('completed', 'processing')).exists():
        return True
    return uses.filter(status='pending', created_at__gte=now - PENDING_HOLD).exists()


def creditable_trial_payment(child, *, branch_id, today: date | None = None, now=None) -> Payment | None:
    """
    The paid trial this child may still cash in for a registration in `branch_id`:
    charged, inside the window, and not already credited. The oldest one first, so
    a parent who paid for two trials loses neither by the order they register.
    """
    if child is None or branch_id is None:
        return None
    today = today or timezone.localdate()
    earliest = today - timedelta(days=TRIAL_CREDIT_WINDOW_DAYS)
    rows = (
        Payment.objects
        .filter(
            child__in=_candidate_children(child),
            payment_type='one_time',
            status='completed',
            trial_lesson_date__isnull=False,
            trial_lesson_date__gte=earliest,
            trial_lesson_date__lte=today,
            final_amount__gt=0,
        )
        .filter(Q(branch_id=branch_id) | Q(lesson__course__branch_id=branch_id))
        .select_related('lesson', 'lesson__course')
        .order_by('trial_lesson_date', 'created_at')
    )
    for trial in rows:
        if not _is_spent(trial, now=now):
            return trial
    return None


def quote_trial_credit(child, *, branch_id, first_charge: Decimal, today: date | None = None) -> dict:
    """
    What to take off this first charge, and the sentence the parent reads.

    `amount` is capped at the charge itself: a trial worth more than the first
    month simply zeroes it, with no leftover (the owner's rule).
    """
    empty = {'amount': Decimal('0.00'), 'source': None, 'trial_paid': Decimal('0.00'), 'trial_date': None, 'reason': ''}
    charge = money(first_charge)
    if charge <= 0:
        return empty
    trial = creditable_trial_payment(child, branch_id=branch_id, today=today)
    if trial is None:
        return empty
    paid = money(trial.final_amount)
    amount = min(paid, charge)
    if amount <= 0:
        return empty
    when = trial.trial_lesson_date.strftime('%d/%m/%Y') if trial.trial_lesson_date else ''
    reason = f'שמנו לב שכבר הייתם אצלנו בשיעור ניסיון ב־{when} ושילמתם עליו ₪{shekels(paid)} — הסכום מקוזז מהתשלום הראשון.'
    if amount < paid:
        reason = (
            f'שמנו לב שכבר הייתם אצלנו בשיעור ניסיון ב־{when} ושילמתם עליו ₪{shekels(paid)} — '
            f'מקוזזים ₪{shekels(amount)}, עד גובה התשלום הראשון.'
        )
    return {'amount': amount, 'source': trial, 'trial_paid': paid, 'trial_date': trial.trial_lesson_date, 'reason': reason}


def credit_for_lesson(child, lesson, *, first_charge: Decimal, today: date | None = None) -> dict:
    """`quote_trial_credit` for a registration to `lesson` — the branch comes from its course."""
    branch_id = lesson.course.branch_id if lesson is not None and lesson.course_id else None
    return quote_trial_credit(child, branch_id=branch_id, first_charge=first_charge, today=today)


def describe(quote: dict) -> dict:
    """The credit as it travels to a client (widget, CRM dialog, card-link page)."""
    return {
        'trial_credit_amount': float(quote['amount']),
        'trial_credit_paid': float(quote['trial_paid']),
        'trial_credit_date': quote['trial_date'].isoformat() if quote.get('trial_date') else None,
        'trial_credit_reason': quote['reason'],
    }
