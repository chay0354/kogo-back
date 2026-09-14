"""
Standing-order card link: signed link → new card → fix token + STO.

Two things the office can ask this link to do, and the token says which:

- `renew`     — the standing order stopped or missed months. Charge exactly the
                months that were never collected, each at the standing order's
                full monthly amount, and put the order back on the new card.
- `card_only` — replace the card on a live standing order and charge NOTHING,
                even when a month is outstanding. `next_billing_date` is left
                alone, so the monthly run still collects it on its own date.

The mode and the amount live *inside* the signed token, never in a query
string: what a link will charge cannot be changed by editing the URL. A token
with no mode is a link issued before this existed and behaves exactly as it did.
"""
from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable

from django.core.signing import BadSignature, SignatureExpired, dumps, loads
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.enrollment_whatsapp import build_enrollment_whatsapp_context
from apps.core.manychat_service import ManyChatService
from apps.core.password_reset_email import crm_frontend_url
from apps.core.payment_service import JERUSALEM_TZ, PaymentService, subscription_tranzila_items
from apps.core.tranzila_service import TranzilaService, extract_card_token
from apps.customers.models import Payment, RecurringPayment, TranzilaTransaction

logger = logging.getLogger(__name__)

SIGN_SALT = 'kogo-card-update'
CARD_UPDATE_TOKEN_MAX_AGE = 14 * 24 * 3600

MODE_RENEW = 'renew'
MODE_CARD_ONLY = 'card_only'
CARD_UPDATE_MODES = (MODE_RENEW, MODE_CARD_ONLY)

MAX_RENEW_MONTHS = 24
MAX_RENEW_AMOUNT = Decimal('50000.00')

# A renew claim younger than this is another submit still at the gateway.
RENEW_CLAIM_STALE_AFTER = timedelta(seconds=90)

HEBREW_MONTHS = (
    'ינואר', 'פברואר', 'מרץ', 'אפריל', 'מאי', 'יוני',
    'יולי', 'אוגוסט', 'ספטמבר', 'אוקטובר', 'נובמבר', 'דצמבר',
)


class CardUpdateError(ValueError):
    def __init__(self, message: str, *, already_done: bool = False):
        super().__init__(message)
        self.already_done = already_done


def _recurring_qs():
    return RecurringPayment.objects.select_related(
        'child',
        'child__family',
        'initial_payment',
        'initial_payment__lesson',
        'initial_payment__lesson__course',
        'initial_payment__lesson__course__branch',
        'initial_payment__bundle',
        'initial_payment__branch',
    )


def _stamp(recurring: RecurringPayment) -> str:
    updated = recurring.updated_at
    if timezone.is_aware(updated):
        updated = timezone.localtime(updated)
    return updated.isoformat(timespec='microseconds')


def month_key(day: date) -> str:
    return f'{day:%Y-%m}'


def month_label(day: date) -> str:
    return f'{HEBREW_MONTHS[day.month - 1]} {day.year}'


def months_label(months: Iterable[date]) -> str:
    """'ספטמבר, אוקטובר' — the year is dropped while every month shares one."""
    rows = list(months)
    if not rows:
        return ''
    years = {row.year for row in rows}
    if len(years) == 1 and years.pop() == timezone.now().astimezone(JERUSALEM_TZ).year:
        return ', '.join(HEBREW_MONTHS[row.month - 1] for row in rows)
    return ', '.join(month_label(row) for row in rows)


def _first_of(day: date) -> date:
    return date(day.year, day.month, 1)


def _month_from_key(raw: Any) -> date | None:
    try:
        year, month = str(raw).split('-')
        return date(int(year), int(month), 1)
    except (ValueError, TypeError):
        return None


def build_card_update_token(
    recurring: RecurringPayment,
    *,
    mode: str = '',
    amount: Decimal | None = None,
    months: Iterable[date] | None = None,
) -> str:
    """
    The signed payload the public page is read from.

    `mode`/`amount`/`months` are signed in, never carried as query parameters:
    editing the URL cannot change what the card will be charged. Called with no
    mode it produces exactly the payload every link before this one carried.
    """
    payload: dict[str, Any] = {'id': str(recurring.id), 'v': _stamp(recurring)}
    if mode:
        if mode not in CARD_UPDATE_MODES:
            raise CardUpdateError('סוג קישור לא מוכר')
        payload['m'] = mode
        if mode == MODE_RENEW:
            payload['a'] = str(Decimal(str(amount or '0')).quantize(Decimal('0.01')))
            payload['mo'] = [month_key(row) for row in (months or [])]
    # Colons break Next.js / WhatsApp URL-button path segments.
    return dumps(payload, salt=SIGN_SALT).replace(':', '~')


def card_update_public_url(
    recurring: RecurringPayment,
    *,
    mode: str = '',
    amount: Decimal | None = None,
    months: Iterable[date] | None = None,
) -> str:
    token = build_card_update_token(recurring, mode=mode, amount=amount, months=months)
    return f'{crm_frontend_url()}/update-card/{token}'


def format_sto_amount(amount) -> str:
    value = Decimal(str(amount)).quantize(Decimal('0.01'))
    if value == value.to_integral():
        return str(int(value))
    return f'{value:.2f}'


def _lesson_for(recurring: RecurringPayment):
    initial = recurring.initial_payment
    return initial.lesson if initial else None


@dataclass
class CardUpdateIntent:
    """What one signed link is allowed to do. Every field comes from the signature."""

    recurring: RecurringPayment
    already_done: bool = False
    mode: str = ''
    amount: Decimal | None = None
    months: list[date] = field(default_factory=list)

    @property
    def is_renew(self) -> bool:
        return self.mode == MODE_RENEW

    @property
    def is_card_only(self) -> bool:
        return self.mode == MODE_CARD_ONLY


def resolve_card_update_intent(token: str) -> CardUpdateIntent:
    raw = (token or '').strip().replace('~', ':')
    if not raw:
        raise CardUpdateError('קישור לא תקין')
    try:
        payload = loads(raw, salt=SIGN_SALT, max_age=CARD_UPDATE_TOKEN_MAX_AGE)
    except SignatureExpired as exc:
        raise CardUpdateError('פג תוקף הקישור. בקשו מהמשרד קישור חדש.') from exc
    except BadSignature as exc:
        raise CardUpdateError('קישור לא תקין') from exc

    payload = payload or {}
    rec_id = str(payload.get('id') or '').strip()
    stamp = str(payload.get('v') or '').strip()
    if not rec_id:
        raise CardUpdateError('קישור לא תקין')

    recurring = _recurring_qs().filter(id=rec_id).first()
    if not recurring:
        raise CardUpdateError('קישור לא תקין')
    if recurring.status == 'cancelled':
        raise CardUpdateError('הוראת הקבע בוטלה. פנו למשרד.')

    mode = str(payload.get('m') or '').strip()
    if mode and mode not in CARD_UPDATE_MODES:
        # A signed payload naming a mode this build does not know is not a link
        # to guess at: it must never fall back to "charge what looks due".
        raise CardUpdateError('קישור לא תקין')
    amount: Decimal | None = None
    months: list[date] = []
    if mode == MODE_RENEW:
        try:
            amount = Decimal(str(payload.get('a') or '0')).quantize(Decimal('0.01'))
        except (InvalidOperation, ValueError) as exc:
            raise CardUpdateError('קישור לא תקין') from exc
        if amount < Decimal('0') or amount > MAX_RENEW_AMOUNT:
            raise CardUpdateError('קישור לא תקין')
        months = [row for row in (_month_from_key(key) for key in payload.get('mo') or []) if row]
        months = sorted(set(months))
        if not months or len(months) > MAX_RENEW_MONTHS:
            raise CardUpdateError('קישור לא תקין')

    already_done = False
    # A link with no mode is guarded by this stamp alone — `updated_at` at the
    # moment it was issued — so it keeps that rule exactly.
    #
    # A link that names its mode does not, and must not: any save on the row
    # (the monthly run charging it, a pending amount being promoted, a manager
    # editing it) moves the stamp, and a card-swap link that dies because the
    # standing order was billed is a link the office cannot rely on. What stops
    # a replay is not the stamp but the money itself — `renew` recomputes which
    # of *its own* months are still outstanding and settles only those, and
    # `card_only` never charges anything at all.
    if not mode and _stamp(recurring) != stamp:
        if recurring.status == 'active':
            already_done = True
        else:
            raise CardUpdateError('הקישור כבר לא בתוקף. בקשו מהמשרד קישור חדש.')
    return CardUpdateIntent(
        recurring=recurring, already_done=already_done, mode=mode, amount=amount, months=months,
    )


def resolve_card_update_token(token: str) -> tuple[RecurringPayment, bool]:
    intent = resolve_card_update_intent(token)
    return intent.recurring, intent.already_done


def preview_payload(
    recurring: RecurringPayment,
    *,
    already_done: bool = False,
    intent: CardUpdateIntent | None = None,
) -> dict:
    """
    What the parent's page shows before a digit is typed.

    `headline` is the one line that says which of the two things this link does,
    and it is built here rather than in the page so the wording a card is entered
    under is the same wording the server will act on.
    """
    lesson = _lesson_for(recurring)
    course_name = ''
    branch_name = ''
    if lesson and lesson.course_id:
        course_name = lesson.course.name
        if lesson.course.branch_id:
            branch_name = lesson.course.branch.name
    child = recurring.child
    mode = intent.mode if intent else ''

    if mode == MODE_CARD_ONLY:
        settle: list[date] = []
        charge = Decimal('0.00')
    elif mode == MODE_RENEW and not already_done:
        plan = plan_renew_charge(recurring, months=intent.months, amount=intent.amount or Decimal('0'))
        settle = plan['settle']
        charge = plan['amount']
    elif mode == MODE_RENEW:
        settle = []
        charge = Decimal('0.00')
    else:
        settle = [] if already_done or not _needs_catchup_charge(recurring) else [
            _first_of(recurring.next_billing_date or timezone.now().astimezone(JERUSALEM_TZ).date())
        ]
        charge = Decimal(str(recurring.amount)).quantize(Decimal('0.01'))

    will_charge = (not already_done) and bool(settle) and charge >= Decimal('1.00')
    if not will_charge:
        settle = []
        charge = Decimal('0.00')
    covered = months_label(settle)

    if mode == MODE_CARD_ONLY:
        headline = 'עדכון פרטי אשראי בלבד — לא יבוצע חיוב'
    elif will_charge and mode == MODE_RENEW:
        headline = f'יחויב ₪{format_sto_amount(charge)} — חידוש הוראת קבע עבור {covered}'
    elif will_charge:
        headline = f'יחויב ₪{format_sto_amount(charge)} — החיוב החודשי שלא נגבה'
    elif already_done:
        headline = 'הכרטיס כבר עודכן — לא יבוצע חיוב'
    else:
        headline = 'עדכון פרטי אשראי בלבד — לא יבוצע חיוב'

    return {
        'ok': True,
        'already_done': already_done,
        'mode': mode,
        'child_name': child.full_name if child else '',
        'course_name': course_name,
        'branch_name': branch_name,
        'amount': str(recurring.amount),
        'amount_label': format_sto_amount(recurring.amount),
        'charge_amount': str(charge),
        'charge_amount_label': format_sto_amount(charge),
        'months': [{'month': month_key(row), 'label': month_label(row)} for row in settle],
        'months_label': covered,
        'headline': headline,
        'next_billing_date': recurring.next_billing_date.isoformat() if recurring.next_billing_date else None,
        'will_charge': will_charge,
    }


def send_card_update_whatsapp(recurring: RecurringPayment) -> dict:
    lesson = _lesson_for(recurring)
    if not lesson or not recurring.child_id:
        return {'sent': False, 'reason': 'missing_lesson_or_child'}

    ctx = build_enrollment_whatsapp_context(child=recurring.child, lesson=lesson)
    if not ctx:
        return {'sent': False, 'reason': 'no_parent_phone'}

    token = build_card_update_token(recurring)
    lookup_names = ctx.pop('lookup_names', None)
    extra_fields = {
        'kogo_card_update_url': f'{crm_frontend_url()}/update-card/{token}',
        'kogo_card_update_token': token,
        'kogo_amount': format_sto_amount(recurring.amount),
        'kogo_support_phone': '050-9424755',
    }
    return ManyChatService().notify_registration(
        kind=ManyChatService.REGISTRATION_KIND_CARD_UPDATE,
        lookup_names=lookup_names,
        extra_fields=extra_fields,
        **ctx,
    )


def send_card_update_for_failed(
    *,
    ids: Iterable[str] | None = None,
    limit: int = 80,
) -> dict:
    qs = (
        _recurring_qs()
        .filter(status='failed')
        .order_by('next_billing_date', 'created_at')
    )
    id_list = [str(value).strip() for value in (ids or []) if str(value).strip()]
    if id_list:
        qs = qs.filter(id__in=id_list)
    rows = list(qs[: max(1, min(int(limit or 80), 200))])

    sent = 0
    failed = 0
    results: list[dict] = []
    for recurring in rows:
        result = send_card_update_whatsapp(recurring)
        row = {
            'id': str(recurring.id),
            'child_name': recurring.child.full_name if recurring.child_id else '',
            'sent': bool(result.get('sent')),
            'reason': result.get('reason'),
            'error': result.get('error'),
        }
        results.append(row)
        if row['sent']:
            sent += 1
        else:
            failed += 1
    return {
        'checked': len(rows),
        'sent': sent,
        'failed': failed,
        'results': results,
    }


def _next_month_first(from_day: date) -> date:
    if from_day.month == 12:
        return date(from_day.year + 1, 1, 1)
    return date(from_day.year, from_day.month + 1, 1)


def _paid_until(end_of_charge_month: date) -> date:
    last_day = calendar.monthrange(end_of_charge_month.year, end_of_charge_month.month)[1]
    return date(end_of_charge_month.year, end_of_charge_month.month, last_day)


def _needs_catchup_charge(recurring: RecurringPayment) -> bool:
    today = timezone.now().astimezone(JERUSALEM_TZ).date()
    due = recurring.next_billing_date
    if due and due > today:
        return False
    charge_month = due or today
    if recurring.last_charge_date and recurring.last_charge_date.year == charge_month.year and recurring.last_charge_date.month == charge_month.month:
        return False
    lesson = _lesson_for(recurring)
    paid = Payment.objects.filter(
        child=recurring.child,
        payment_type='recurring_subscription',
        status='completed',
        payment_date__year=charge_month.year,
        payment_date__month=charge_month.month,
    )
    if lesson:
        paid = paid.filter(lesson=lesson)
    return not paid.exists()


# ---------------------------------------------------------------------------
# renew — the months that were never collected, each at the full monthly amount
# ---------------------------------------------------------------------------

def missed_months(recurring: RecurringPayment, *, today: date | None = None) -> list[date]:
    """
    Every month this standing order owes, as first-of-month dates, oldest first.

    The same question `_needs_catchup_charge` asks — is this month's subscription
    on record? — asked of every month from `next_billing_date` up to the current
    one. Nothing is prorated and nothing is invented: a month counts as collected
    when `last_charge_date` falls in it, or a completed `recurring_subscription`
    payment for this child (and this lesson) is dated inside it.
    """
    if today is None:
        today = timezone.now().astimezone(JERUSALEM_TZ).date()
    due = recurring.next_billing_date
    # Not due yet is not missed — the same first line `_needs_catchup_charge` has.
    if due and due > today:
        return []
    start = _first_of(due or today)
    end = _first_of(today)
    if start > end:
        return []

    wanted: list[date] = []
    cursor = start
    while cursor <= end and len(wanted) < MAX_RENEW_MONTHS:
        wanted.append(cursor)
        cursor = _next_month_first(cursor)

    last = recurring.last_charge_date
    lesson = _lesson_for(recurring)
    paid = Payment.objects.filter(
        child=recurring.child,
        payment_type='recurring_subscription',
        status='completed',
        payment_date__date__gte=start,
        payment_date__date__lt=_next_month_first(end),
    )
    if lesson:
        paid = paid.filter(lesson=lesson)
    # The filter above asks Postgres for dates in Asia/Jerusalem (Django's own
    # timezone), so the months are counted in the same clock. Reading the month
    # off the stored UTC value instead would put a payment taken at 00:30 on the
    # 1st into the month before, and hand the office a month to charge twice.
    collected = {
        month_key(row.astimezone(JERUSALEM_TZ))
        for row in paid.values_list('payment_date', flat=True)
        if row
    }
    if last:
        collected.add(month_key(last))
    return [row for row in wanted if month_key(row) not in collected]


def renew_quote(recurring: RecurringPayment, *, today: date | None = None) -> dict:
    """What a renewal link would ask for: the missed months and their exact total."""
    months = missed_months(recurring, today=today)
    monthly = Decimal(str(recurring.amount or '0')).quantize(Decimal('0.01'))
    return {
        'months': months,
        'monthly_amount': monthly,
        # No proration, no registration fee: the full monthly figure, once per month.
        'amount': (monthly * len(months)).quantize(Decimal('0.01')),
    }


def plan_renew_charge(
    recurring: RecurringPayment,
    *,
    months: Iterable[date],
    amount: Decimal,
    today: date | None = None,
) -> dict:
    """
    What the token still has the right to settle, recomputed at charge time.

    The token names the months the office showed the parent. Between the link
    being made and the parent paying, the monthly run may have collected one of
    them; a second submit of the same link may already have settled all of them.
    Only months that are *still* outstanding are charged for, so a month can
    never be paid twice — and never a month the parent was not shown.
    """
    wanted = sorted(set(months))
    outstanding = {month_key(row) for row in missed_months(recurring, today=today)}
    settle = [row for row in wanted if month_key(row) in outstanding]
    total = Decimal(str(amount or '0')).quantize(Decimal('0.01'))
    if not settle or total <= 0:
        return {'settle': [], 'amount': Decimal('0.00'), 'dropped': [row for row in wanted if row not in settle]}
    if len(settle) == len(wanted):
        charge = total
    else:
        per_month = (total / Decimal(len(wanted))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        charge = min(total, (per_month * len(settle)).quantize(Decimal('0.01')))
    return {'settle': settle, 'amount': charge, 'dropped': [row for row in wanted if row not in settle]}


def _renew_claim_key(recurring_id, settle: list[date]) -> str:
    return f'card_update_renew_{recurring_id}_{month_key(settle[0])}_{month_key(settle[-1])}'


def _claim_renew(recurring_id, settle: list[date]) -> TranzilaTransaction:
    """
    Take the right to charge this exact run of months, before the gateway is called.

    `idempotency_key` is unique, so two submits racing each other — a parent who
    double-taps, two tabs — cannot both reach Tranzila: the second loses the
    insert and is told to wait. The row is dropped again on a decline so another
    card may be tried; an answer that never came back keeps it, and the parent is
    sent to the office rather than allowed to pay twice.
    """
    key = _renew_claim_key(recurring_id, settle)
    existing = TranzilaTransaction.objects.filter(idempotency_key=key).first()
    if existing is not None:
        if existing.is_successful:
            raise CardUpdateError('החיוב כבר בוצע.', already_done=True)
        if timezone.now() - existing.request_timestamp < RENEW_CLAIM_STALE_AFTER:
            raise CardUpdateError('החיוב בעיבוד. המתינו רגע ואל תשלחו שוב.')
        raise CardUpdateError('קיים חיוב שלא הסתיים. פנו למשרד לפני ניסיון נוסף.')
    try:
        with transaction.atomic():
            return TranzilaTransaction.objects.create(
                transaction_id='',
                confirmation_code='',
                transaction_type='recurring_charge',
                response_code='',
                response_message='',
                request_data={'months': [month_key(row) for row in settle]},
                response_data={},
                idempotency_key=key,
                is_successful=False,
            )
    except IntegrityError as exc:
        raise CardUpdateError('החיוב בעיבוד. המתינו רגע ואל תשלחו שוב.') from exc


def apply_new_card(
    recurring: RecurringPayment,
    card: dict[str, Any],
    *,
    intent: CardUpdateIntent | None = None,
) -> dict:
    lesson = _lesson_for(recurring)
    if not lesson:
        raise CardUpdateError('לא נמצא חוג להוראת הקבע. פנו למשרד.')
    if recurring.status == 'cancelled':
        raise CardUpdateError('הוראת הקבע בוטלה. פנו למשרד.')

    today = timezone.now().astimezone(JERUSALEM_TZ).date()
    child = recurring.child
    family = child.family
    monthly = Decimal(str(recurring.amount)).quantize(Decimal('0.01'))
    mode = intent.mode if intent else ''

    # What this link is allowed to charge, and for which months. `settle` holds
    # first-of-month dates; its last entry is what the dates below move past.
    if mode == MODE_CARD_ONLY:
        # A card swap, and nothing else — an outstanding month stays outstanding
        # so the monthly run collects it on its own date, exactly once.
        settle: list[date] = []
        amount = Decimal('0.00')
        guard_key = f'card-update-verify-{recurring.id}'
    elif mode == MODE_RENEW:
        plan = plan_renew_charge(
            recurring, months=intent.months, amount=intent.amount or Decimal('0'), today=today,
        )
        settle = plan['settle']
        amount = plan['amount']
        guard_key = (
            f'card-update-{recurring.id}-{month_key(settle[0])}-{month_key(settle[-1])}'
            if settle else f'card-update-verify-{recurring.id}'
        )
    else:
        # Every link issued before modes existed: one month, at the standing figure.
        charge_month = recurring.next_billing_date or today
        settle = [_first_of(charge_month)] if _needs_catchup_charge(recurring) else []
        amount = monthly
        guard_key = f'card-update-{recurring.id}-{charge_month:%Y-%m}'

    will_charge = bool(settle) and amount >= Decimal('1.00')
    if not will_charge:
        settle = []

    claim = None
    if will_charge and mode == MODE_RENEW:
        # Claimed before the gateway is touched: whatever happens next, a second
        # submit for these months cannot reach Tranzila behind this one's back.
        try:
            claim = _claim_renew(recurring.id, settle)
        except CardUpdateError as exc:
            if not exc.already_done:
                raise
            # These months are already paid for. The parent still typed a card,
            # so keep it — and take nothing.
            will_charge = False
            settle = []
            amount = Decimal('0.00')
            guard_key = f'card-update-verify-{recurring.id}'

    tranzila = TranzilaService.production()
    label = f'{lesson.course.name} - {child.full_name}'
    payment = None

    if will_charge:
        covered = months_label(settle)
        renewing = mode == MODE_RENEW
        description = (
            f'חידוש הוראת קבע - {lesson.course.name} - {child.full_name} · עבור {covered}'
            if renewing else f'מנוי חודשי - {lesson.course.name} - {child.full_name}'
        )
        items = subscription_tranzila_items(
            label=f'{label} · {covered}' if renewing else label,
            prorated_lesson=amount,
            registration_fee=Decimal('0'),
            prorated=False,
        )
        payment = Payment.objects.create(
            child=child,
            family=family,
            parent=family.parents.filter(is_primary=True).first() if family else None,
            branch=lesson.course.branch,
            lesson=lesson,
            bundle=recurring.initial_payment.bundle if recurring.initial_payment else None,
            payment_type='recurring_subscription',
            status='pending',
            # A renewal is a sum of whole months: its own base, with no single
            # month's discount copied onto it.
            base_amount=amount if renewing else (recurring.base_amount or amount),
            discount_amount=Decimal('0.00') if renewing else (recurring.discount_amount or Decimal('0.00')),
            final_amount=amount,
            registration_fee=Decimal('0.00'),
            description=description,
        )
        result = tranzila.charge_with_card(
            card_number=card['card_number'],
            expiry_month=card['expiry_month'],
            expiry_year=card['expiry_year'],
            cvv=card['cvv'],
            card_holder_id=card.get('card_holder_id') or '',
            amount=amount,
            description=payment.description,
            items=items,
            duplicate_guard_key=guard_key,
        )
    else:
        result = tranzila.verify_card(
            card_number=card['card_number'],
            expiry_month=card['expiry_month'],
            expiry_year=card['expiry_year'],
            cvv=card['cvv'],
            card_holder_id=card.get('card_holder_id') or '',
            amount=monthly if monthly >= Decimal('1.00') else Decimal('1.00'),
            description=f'עדכון כרטיס - {child.full_name}',
            duplicate_guard_key=guard_key,
        )

    if not result.get('success'):
        if payment is not None:
            payment.status = 'failed'
            payment.failure_reason = result.get('error', 'התשלום נכשל')
            payment.save(update_fields=['status', 'failure_reason', 'updated_at'])
        # A declined card took no money, so the months stay claimable: drop the
        # claim and let the parent try another card on the same link.
        if claim is not None:
            TranzilaTransaction.objects.filter(id=claim.id, is_successful=False).delete()
        raise CardUpdateError(result.get('error') or 'התשלום נכשל')

    token = (result.get('token') or '').strip() or extract_card_token(
        result.get('raw_response') if isinstance(result.get('raw_response'), dict) else {},
        result,
    )
    if not token:
        if payment is not None:
            payment.status = 'failed'
            payment.failure_reason = 'לא התקבל טוקן כרטיס חדש'
            payment.save(update_fields=['status', 'failure_reason', 'updated_at'])
        raise CardUpdateError('הכרטיס חויב אבל לא התקבל טוקן. פנו למשרד לפני ניסיון נוסף.')

    service = PaymentService()
    with transaction.atomic():
        locked = (
            RecurringPayment.objects
            .select_for_update(of=('self',))
            .select_related('child', 'initial_payment', 'initial_payment__lesson')
            .get(id=recurring.id)
        )
        locked.tranzila_token = token
        locked.card_expire_month = card['expiry_month']
        locked.card_expire_year = card['expiry_year']
        locked.status = 'active'
        locked.cancellation_reason = ''
        update_fields = [
            'tranzila_token',
            'card_expire_month',
            'card_expire_year',
            'status',
            'cancellation_reason',
            'updated_at',
        ]

        if will_charge and payment is not None:
            settled_through = settle[-1]
            payment.status = 'completed'
            payment.payment_date = timezone.now()
            payment.save(update_fields=['status', 'payment_date', 'updated_at'])
            if claim is not None:
                # The claim row taken before the charge becomes its record; a
                # second row would collide with its own unique key.
                claim.transaction_id = result.get('transaction_id', '')
                claim.confirmation_code = result.get('confirmation_code', '')
                claim.response_code = result.get('response_code', '000')
                claim.response_data = result.get('raw_response', {}) or {}
                claim.is_successful = True
                claim.response_timestamp = timezone.now()
                claim.save(update_fields=[
                    'transaction_id', 'confirmation_code', 'response_code',
                    'response_data', 'is_successful', 'response_timestamp',
                ])
                tranzila_txn = claim
            else:
                tranzila_txn = TranzilaTransaction.objects.create(
                    transaction_id=result.get('transaction_id', ''),
                    confirmation_code=result.get('confirmation_code', ''),
                    transaction_type='recurring_charge',
                    response_code=result.get('response_code', '000'),
                    response_message='',
                    request_data={},
                    response_data=result.get('raw_response', {}) or {},
                    idempotency_key=f'card_update_{locked.id}_{today.isoformat()}',
                    is_successful=True,
                    response_timestamp=timezone.now(),
                )
            payment.tranzila_transaction = tranzila_txn
            payment.save(update_fields=['tranzila_transaction'])
            locked.last_charge_date = today
            # Past every month just paid, and never backwards: the monthly run
            # reads this date, and a month behind it is a month it charges again.
            settled_next = _next_month_first(settled_through)
            locked.next_billing_date = (
                max(locked.next_billing_date, settled_next) if locked.next_billing_date else settled_next
            )
            update_fields.extend(['last_charge_date', 'next_billing_date'])
            child.status = 'active'
            paid_until = _paid_until(settled_through)
            child.paid_until_date = (
                max(child.paid_until_date, paid_until) if child.paid_until_date else paid_until
            )
            child.save(update_fields=['status', 'paid_until_date', 'updated_at'])
        else:
            if child.status == 'payment_problem':
                child.status = 'active'
                child.save(update_fields=['status', 'updated_at'])

        locked.save(update_fields=update_fields)

    if will_charge and payment is not None:
        # After the charge is on record, never inside it: a receipt that failed
        # there rolled back the payment, the new token and next_billing_date, so
        # the parent saw an error for a card that had been charged, and tried
        # again. `check_invoices` issues a receipt that is missing.
        try:
            service._create_invoice_from_payment(payment, tranzila_txn)
        except Exception:
            logger.exception('Receipt not issued for card-update charge %s (the charge is recorded)', payment.id)

    covered_label = months_label(settle) if will_charge else ''
    return {
        'success': True,
        'charged': will_charge,
        'mode': mode,
        'amount': str(amount) if will_charge else '0',
        'amount_label': format_sto_amount(amount) if will_charge else '0',
        'months': [month_key(row) for row in settle],
        'months_label': covered_label,
        'message': (
            f'שולם ₪{format_sto_amount(amount)} עבור {covered_label}. הוראת הקבע פעילה עם הכרטיס החדש.'
            if will_charge
            else 'פרטי האשראי עודכנו. לא בוצע חיוב.'
        ),
        'next_billing_date': (
            locked.next_billing_date.isoformat() if locked.next_billing_date else None
        ),
    }
