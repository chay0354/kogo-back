"""Making and running a payment split between cards (models: split_models.py).

    divide(total, parts, wanted)        the shares of a figure, adding up to it to the agora
    create_split(...)                    a sign-up paid in parts: one card link per payer
    create_split_from_existing(...)      a child who already pays on one card: that card
                                         stays the first payer, the others join next cycle
    share_quote(share, quote, today)     one payer's part of a card link's quote
    create_share_recurring(...)          the standing order of one payer, after their card
    after_share_activated(share)         an existing order steps down to its part
    split_payload(split)                 the split as the CRM shows it

The office makes a split; nothing here charges a card. Each payer's link is
paid through the ordinary card-link flow (card_link.apply_card_link), which
asks this module for that payer's figures.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import ROUND_DOWN, Decimal

from django.db import transaction
from django.utils import timezone

from apps.customers.split_models import MAX_SPLIT_SHARES, PaymentSplit, PaymentSplitShare

logger = logging.getLogger(__name__)

CENT = Decimal('0.01')


class SplitError(ValueError):
    pass


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT)


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def divide(total, parts: int, wanted: list | None = None) -> list[Decimal]:
    """
    `total` in `parts` shares that add up to it exactly.

    Equal by default, the agorot left over on the last share (₪335 in three:
    111.67 + 111.67 + 111.66). `wanted` is the office's own figures; they must
    add up to the total, and none may be below ₪1.
    """
    total = money(total)
    if parts < 2 or parts > MAX_SPLIT_SHARES:
        raise SplitError(f'אפשר לחלק בין 2 ל־{MAX_SPLIT_SHARES} משלמים')
    if wanted:
        if len(wanted) != parts:
            raise SplitError('מספר הסכומים לא תואם למספר המשלמים')
        try:
            shares = [money(value) for value in wanted]
        except (ArithmeticError, ValueError, TypeError):
            raise SplitError('סכום לא תקין')
        if any(share < Decimal('1.00') for share in shares):
            raise SplitError('כל משלם משלם לפחות ₪1')
        if sum(shares) != total:
            raise SplitError(f'הסכומים צריכים להסתכם ב־₪{total} (עכשיו ₪{sum(shares)})')
        return shares
    one = (total / parts).quantize(CENT, rounding=ROUND_DOWN)
    left = int((total - one * parts) / CENT)
    # The agorot left over go one each to the first payers: 111.67, 111.67, 111.66.
    return [one + (CENT if index < left else Decimal('0.00')) for index in range(parts)]


def divide_like(total, weights: list[Decimal]) -> list[Decimal]:
    """`total` in the proportions of `weights` (the monthly shares), adding up to it exactly."""
    total = money(total)
    whole = sum(weights)
    if total == 0 or whole == 0:
        return [Decimal('0.00')] * len(weights)
    parts = [money(total * weight / whole) for weight in weights]
    parts[-1] = total - sum(parts[:-1])
    return parts


# ---------------------------------------------------------------------------
# Payers
# ---------------------------------------------------------------------------

def _payer(child, raw: dict) -> dict:
    """{parent, name, phone, email} from a parent of the family, or from what the office typed."""
    from apps.customers.models import Parent

    raw = raw or {}
    parent = None
    parent_id = str(raw.get('parent_id') or '').strip()
    if parent_id:
        parent = Parent.objects.filter(id=parent_id, family_id=child.family_id).first()
        if parent is None:
            raise SplitError('ההורה שנבחר אינו במשפחה של הילד')
    name = str(raw.get('name') or '').strip() or (f'{parent.first_name} {parent.last_name}'.strip() if parent else '')
    phone = str(raw.get('phone') or '').strip() or (parent.phone if parent else '')
    email = str(raw.get('email') or '').strip() or (parent.email if parent else '')
    if not name:
        raise SplitError('לכל משלם צריך שם')
    if not phone:
        raise SplitError(f'ל{name} חסר טלפון — אליו נשלח הקישור')
    from apps.core.direction_marks import strip_direction_marks

    return {
        'parent': parent,
        'name': strip_direction_marks(name)[:200],
        'phone': strip_direction_marks(phone)[:20],
        'email': strip_direction_marks(email)[:254],
    }


def _primary_payer(child, recurring) -> dict:
    """Who pays an existing standing order: its first charge's parent, else the family's primary."""
    family = child.family
    parent = None
    initial = recurring.initial_payment if recurring.initial_payment_id else None
    if initial is not None and initial.parent_id:
        parent = initial.parent
    if parent is None and family is not None:
        parent = family.parents.filter(is_primary=True).first() or family.parents.first()
    name = f'{parent.first_name} {parent.last_name}'.strip() if parent else (family.name if family else '')
    return {
        'parent': parent,
        'name': name or 'משלם 1',
        'phone': (parent.phone if parent else '') or (family.phone if family else ''),
        'email': (parent.email if parent else '') or (family.email if family else ''),
    }


# ---------------------------------------------------------------------------
# Making a split
# ---------------------------------------------------------------------------

def _whole_quote(*, child, lesson, bundle, special, include_fee):
    """What one card would be quoted for this class today — the whole that is divided."""
    from apps.customers.card_link import quote_standing_order
    from apps.payment_links.models import CardLink

    probe = CardLink(
        kind=CardLink.KIND_STANDING_ORDER, child=child, lesson=lesson, bundle=bundle,
        include_registration_fee=include_fee, special_monthly_price=special,
    )
    return quote_standing_order(probe)


def _discount_lines(discounts) -> list[dict]:
    return [
        {
            'name': item.name, 'type': item.discount_type, 'value': str(money(item.value)),
            'amount_deducted': str(money(item.value)), 'reason': item.reason,
        }
        for item in discounts
    ]


def create_split(*, child, lesson, bundle, payers: list[dict], amounts: list | None = None,
                 special=None, include_fee: bool = True, created_by=None):
    """
    A sign-up paid in parts. Returns (split, [CardLink per payer]).

    The whole is quoted the way one card would be — price, discounts, special
    price, registration fee — and divided. The links are created, not sent.
    """
    from apps.core.payment_service import child_has_standing_order_for_lessons, lessons_covered_by_selection
    from apps.customers.card_link import CardLinkError
    from apps.payment_links.models import CardLink

    parts = len(payers or [])
    if parts < 2 or parts > MAX_SPLIT_SHARES:
        raise SplitError(f'אפשר לחלק בין 2 ל־{MAX_SPLIT_SHARES} משלמים')
    covered = lessons_covered_by_selection(lesson=lesson, bundle=bundle)
    if child_has_standing_order_for_lessons(child, covered):
        raise SplitError('לילד כבר יש הוראת קבע לחוג הזה — לחלוקה שלה: כרטיס הילד ← תשלומים ← "חלוקה"')
    if _open_split(child, lesson, bundle) is not None:
        raise SplitError('לילד כבר יש חלוקת תשלום פתוחה לחוג הזה')
    who = [_payer(child, raw) for raw in payers]
    try:
        whole = _whole_quote(child=child, lesson=lesson, bundle=bundle, special=special, include_fee=include_fee)
    except CardLinkError as exc:
        raise SplitError(str(exc)) from exc
    monthly = divide(whole['monthly_amount'], parts, amounts)
    fees = divide_like(whole['registration_fee'], monthly)

    with transaction.atomic():
        split = PaymentSplit.objects.create(
            child=child, lesson=lesson, bundle=bundle, mode=PaymentSplit.MODE_NEW,
            monthly_total=money(whole['monthly_amount']), base_amount=money(whole['base_amount']),
            registration_fee_total=money(whole['registration_fee']),
            discount_details=_discount_lines(whole['discounts']),
            special_monthly_price=special, include_registration_fee=include_fee, created_by=created_by,
        )
        links = []
        for position, (payer, amount, fee) in enumerate(zip(who, monthly, fees), start=1):
            share = PaymentSplitShare.objects.create(
                split=split, position=position, payer_parent=payer['parent'], payer_name=payer['name'],
                payer_phone=payer['phone'], payer_email=payer['email'], monthly_amount=amount, registration_fee=fee,
            )
            links.append(CardLink.objects.create(
                kind=CardLink.KIND_STANDING_ORDER, child=child, lesson=lesson, bundle=bundle,
                include_registration_fee=include_fee, special_monthly_price=special,
                branch=lesson.course.branch, split_share=share, created_by=created_by,
            ))
    return split, links


def create_split_from_existing(*, recurring, payers: list[dict], amounts: list | None = None, created_by=None):
    """
    Split a standing order that bills the whole. Returns (split, [CardLink per new payer]).

    The card on it stays: it is the first payer and keeps paying the whole
    until another payer's card is saved (after_share_activated). `payers` are
    the new payers only; `amounts`, when given, are every share's figure —
    the first payer's first.
    """
    from apps.payment_links.models import CardLink

    if recurring.status != 'active':
        raise SplitError('אפשר לחלק רק הוראת קבע פעילה')
    if recurring.split_share_id:
        raise SplitError('הוראת הקבע הזאת כבר חלק מחלוקה')
    if (recurring.tranzila_recurring_index or '').strip():
        raise SplitError('הוראת הקבע הזאת מנוהלת בטרנזילה — אי אפשר לחלק אותה מכאן')
    if not recurring.next_billing_date or recurring.next_billing_date.day != 1:
        raise SplitError('הוראת הקבע הזאת אינה נגבית ב־1 בחודש — יש לטפל בה ידנית')
    if recurring.pending_amount is not None:
        raise SplitError('בהוראת הקבע כבר מתוזמן סכום אחר — יש לטפל בו לפני חלוקה')
    initial = recurring.initial_payment
    if initial is None or initial.lesson_id is None:
        raise SplitError('לא ידוע על איזה חוג הוראת הקבע')
    child = recurring.child
    parts = 1 + len(payers or [])
    if parts < 2 or parts > MAX_SPLIT_SHARES:
        raise SplitError(f'אפשר לחלק בין 2 ל־{MAX_SPLIT_SHARES} משלמים')
    first = _primary_payer(child, recurring)
    who = [first] + [_payer(child, raw) for raw in payers]
    monthly = divide(recurring.amount, parts, amounts)

    with transaction.atomic():
        locked = type(recurring).objects.select_for_update().get(id=recurring.id)
        if locked.split_share_id:
            raise SplitError('הוראת הקבע הזאת כבר חלק מחלוקה')
        split = PaymentSplit.objects.create(
            child=child, lesson=initial.lesson, bundle=initial.bundle, mode=PaymentSplit.MODE_EXISTING,
            monthly_total=money(recurring.amount), base_amount=money(recurring.base_amount or recurring.amount),
            registration_fee_total=Decimal('0.00'),
            discount_details=recurring.discount_details if isinstance(recurring.discount_details, list) else [],
            include_registration_fee=False, source_recurring=locked, created_by=created_by,
        )
        links = []
        for position, (payer, amount) in enumerate(zip(who, monthly), start=1):
            share = PaymentSplitShare.objects.create(
                split=split, position=position, payer_parent=payer['parent'], payer_name=payer['name'],
                payer_phone=payer['phone'] or '', payer_email=payer['email'] or '', monthly_amount=amount,
            )
            if position == 1:
                locked.split_share = share
                locked.save(update_fields=['split_share', 'updated_at'])
                continue
            links.append(CardLink.objects.create(
                kind=CardLink.KIND_STANDING_ORDER, child=child, lesson=initial.lesson, bundle=initial.bundle,
                include_registration_fee=False, branch=initial.lesson.course.branch, split_share=share,
                created_by=created_by,
            ))
    return split, links


def _open_split(child, lesson, bundle):
    """A live split of this child on this class, or None."""
    qs = PaymentSplit.objects.filter(child=child, status=PaymentSplit.STATUS_ACTIVE)
    if bundle is not None:
        return qs.filter(bundle=bundle).first()
    return qs.filter(lesson=lesson, bundle__isnull=True).first()


# ---------------------------------------------------------------------------
# One payer's link
# ---------------------------------------------------------------------------

def share_has_live_order(share) -> bool:
    return share.recurring_payments.filter(status__in=('active', 'paused')).exists()


def share_quote(share, quote: dict, today: date) -> dict:
    """
    One payer's part of a card link's `quote` (card_link.quote_standing_order).

    The figures were fixed when the split was made and are not asked again: a
    payer who opens the link a week later pays what the office showed. The
    first charge is their part of this month (by the lessons left in it) and
    their part of the registration fee; a trial credit comes off the first
    payer's. A payer joining an existing standing order pays nothing now — the
    month was paid — and is billed from its next date.
    """
    from apps.core.payment_service import _compute_prorate
    from apps.customers.discount_service import ApplicableDiscount

    split = share.split
    parts = split.shares.count()
    monthly = money(share.monthly_amount)
    fraction = (monthly / split.monthly_total) if split.monthly_total else Decimal('0')
    base = money(split.base_amount * fraction) if split.base_amount else monthly
    discounts = [
        ApplicableDiscount(
            discount_id=str(line.get('discount_id') or ''), name=str(line.get('name') or 'הנחה'),
            discount_type=str(line.get('type') or ''),
            value=money(Decimal(str(line.get('amount_deducted') or line.get('value') or 0)) * fraction),
            reason=f"{line.get('reason') or line.get('name') or ''} · {share.position} מתוך {parts}".strip(' ·'),
        )
        for line in (split.discount_details or []) if isinstance(line, dict)
    ]
    out = dict(quote)
    out.update({
        'monthly_amount': monthly,
        'base_amount': max(base, monthly),
        'discount_amount': max(Decimal('0.00'), money(base - monthly)),
        'discounts': discounts,
        'description': f"{quote['description']} · חלק {share.position} מתוך {parts}",
        'split_share': share,
    })
    if split.mode == PaymentSplit.MODE_EXISTING:
        source = split.source_recurring
        out.update({
            'registration_fee': Decimal('0.00'), 'prorated_lesson': Decimal('0.00'),
            'trial_credit': Decimal('0.00'), 'trial_credit_source': None, 'trial_credit_reason': '',
            'first_charge': Decimal('0.00'),
            'next_billing_date': (source.next_billing_date if source and source.next_billing_date else quote['next_billing_date']),
        })
        return out
    fee = money(share.registration_fee) if split.include_registration_fee else Decimal('0.00')
    if quote['deferred_first_charge_date'] or monthly <= 0:
        prorated = Decimal('0.00')
    else:
        if split.lesson is None:
            raise SplitError('השיעור של החלוקה כבר לא קיים — פנו למשרד')
        factor, _, _, _ = _compute_prorate(today, split.lesson.day_of_week)
        prorated = max(Decimal('1.00'), money(monthly * factor))
    credit = Decimal('0.00')
    source = None
    reason = ''
    if share.position == 1 and quote.get('trial_credit'):
        credit = min(money(quote['trial_credit']), prorated + fee)
        source = quote.get('trial_credit_source')
        reason = quote.get('trial_credit_reason') or ''
    out.update({
        'registration_fee': fee, 'prorated_lesson': prorated,
        'trial_credit': credit, 'trial_credit_source': source if credit else None, 'trial_credit_reason': reason if credit else '',
        'first_charge': money(prorated + fee - credit),
    })
    return out


def create_share_recurring(*, payment, share, token: str, expiry_month, expiry_year,
                           enrollment_date: date, next_billing_date: date, tranzila_terminal: str = ''):
    """The standing order of one payer, after their card was saved. Returns it (or the one already made)."""
    from apps.customers.models import RecurringPayment

    existing = RecurringPayment.objects.filter(initial_payment=payment).first()
    if existing is not None:
        return existing
    if share_has_live_order(share):
        raise SplitError('למשלם הזה כבר יש הוראת קבע פעילה')
    try:
        month = int(expiry_month) if expiry_month is not None else None
        year = int(expiry_year) if expiry_year is not None else None
    except (TypeError, ValueError):
        month, year = None, None
    return RecurringPayment.objects.create(
        child=payment.child,
        initial_payment=payment,
        split_share=share,
        tranzila_token=token,
        tranzila_terminal=(tranzila_terminal or '').strip(),
        card_expire_month=month,
        card_expire_year=year,
        status='active',
        base_amount=payment.base_amount,
        discount_amount=payment.discount_amount,
        amount=money(share.monthly_amount),
        discount_details=[
            {
                'name': snap.discount_name, 'type': snap.discount_type, 'value': str(snap.discount_value),
                'amount_deducted': str(snap.amount_deducted), 'reason': snap.reason,
            }
            for snap in payment.discount_snapshots.all()
        ],
        billing_day=1,
        start_date=enrollment_date,
        next_billing_date=next_billing_date,
    )


def after_share_activated(share) -> None:
    """
    An existing standing order steps down as the others' cards are saved.

    The first payer pays the whole less what the payers already on board pay,
    from its next billing date — never less before another card is there to
    pay the rest.
    """
    from apps.customers.recurring_amount import schedule_recurring_amount

    split = share.split
    if split.mode != PaymentSplit.MODE_EXISTING or share.position == 1:
        return
    source = split.source_recurring
    if source is None or source.status != 'active':
        return
    others = sum(
        (money(item.monthly_amount) for item in split.shares.exclude(position=1) if share_has_live_order(item)),
        Decimal('0.00'),
    )
    wanted = max(Decimal('1.00'), money(split.monthly_total) - others)
    schedule_recurring_amount(source, wanted)


def payer_message_context(share, ctx: dict | None) -> dict | None:
    """A WhatsApp context (core.enrollment_whatsapp) addressed to this payer instead of the primary parent."""
    if ctx is None or not (share.payer_phone or '').strip():
        return ctx
    out = dict(ctx)
    out['phone'] = share.payer_phone.strip()
    out['parent_name'] = share.payer_name
    names = [share.payer_name] + [word for word in share.payer_name.split() if len(word) >= 2]
    out['lookup_names'] = list(dict.fromkeys(names + list(ctx.get('lookup_names') or [])))
    return out


# ---------------------------------------------------------------------------
# What the CRM shows
# ---------------------------------------------------------------------------

def share_state(share) -> str:
    """'active' / 'failed' / 'waiting' (no card yet) / 'cancelled'."""
    orders = list(share.recurring_payments.all())
    if any(order.status in ('active', 'paused') for order in orders):
        return 'active'
    if any(order.status == 'failed' for order in orders):
        return 'failed'
    if orders:
        return 'cancelled'
    return 'waiting'


def split_payload(split) -> dict:
    shares = list(split.shares.select_related('payer_parent').prefetch_related('recurring_payments', 'card_links'))
    return {
        'id': str(split.id),
        'mode': split.mode,
        'status': split.status,
        'monthly_total': str(split.monthly_total),
        'registration_fee_total': str(split.registration_fee_total),
        'lesson_id': str(split.lesson_id) if split.lesson_id else None,
        'bundle_id': str(split.bundle_id) if split.bundle_id else None,
        'created_at': split.created_at.isoformat() if split.created_at else None,
        'shares': [
            {
                'id': str(share.id),
                'position': share.position,
                'payer_name': share.payer_name,
                'payer_phone': share.payer_phone,
                'payer_parent_id': str(share.payer_parent_id) if share.payer_parent_id else None,
                'monthly_amount': str(share.monthly_amount),
                'registration_fee': str(share.registration_fee),
                'state': share_state(share),
                'card_link_id': next(
                    (str(link.id) for link in sorted(share.card_links.all(), key=lambda row: row.created_at, reverse=True)),
                    None,
                ),
            }
            for share in shares
        ],
    }


def cancel_waiting_split(split) -> None:
    """Close a split nobody has paid into yet (its links are cancelled with it)."""
    from apps.payment_links.models import CardLink

    if any(share_state(share) != 'waiting' for share in split.shares.all()):
        raise SplitError('משלם כבר הזין כרטיס — יש לבטל את הוראות הקבע בכרטיס הילד')
    with transaction.atomic():
        CardLink.objects.filter(split_share__split=split, status=CardLink.STATUS_PENDING).update(
            status=CardLink.STATUS_CANCELLED, updated_at=timezone.now(),
        )
        split.status = PaymentSplit.STATUS_CANCELLED
        split.cancelled_at = timezone.now()
        split.save(update_fields=['status', 'cancelled_at'])
