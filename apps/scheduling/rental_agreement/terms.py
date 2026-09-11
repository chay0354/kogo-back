"""The rental contract as data: everything a contract says, and its fingerprint.

`terms` is a snapshot of what one rental contract states, held in a plain dict
that JSON stores unchanged: strings, ints, None, lists and dicts. Amounts are
strings to the agora ('1416.00'), dates ISO strings, hours 'HH:MM'. The PDF is
drawn from it (generator.generate_tenancy_contract_pdf), a stored contract
keeps it (apps.rentals.models.RentalContract.terms), and phase 3 binds the
tenant's signature to its fingerprint. The keys:

    template_version  the layout and legal text (content.py) it is drawn with
    studio            the studio's business details, from content.py
    tenant            name, company_number, id_number, phone, email, address
    branch            {"name": ...}, or None
    activity          what the studio is used for: the slots' names
    rows              the payment table, one row per weekday a weekly slot
                      repeats on and one per one-time rental:
                      {kind, weekday, date, start_time, end_time, studio, rate, sum}
    period            'monthly', or 'once' for a single one-time rental
    monthly_amount    the amount before VAT
    vat_rate          '0.18'
    vat_amount        monthly_total - monthly_amount, so the parts add up
    monthly_total     the amount with VAT, to the agora (apps.core.vat.add_vat)
    billing_day       the day of the month it is charged on, or None
    start_date, end_date

terms_sha256 is the SHA-256 of the canonical JSON: sorted keys, compact
separators, UTF-8, Decimals as strings. It depends only on what the terms say,
never on the order the keys were written in or on how the database stored
them (Postgres jsonb reorders keys), so a stored contract's fingerprint can be
recomputed at any time and compared.
"""
from __future__ import annotations

import hashlib
import json
from datetime import time as time_cls
from decimal import ROUND_HALF_UP, Decimal

from apps.core.vat import VAT_RATE, add_vat
from apps.scheduling.studio_conflict import event_day_time_pairs

from . import content

# The layout and legal text a snapshot is drawn with. Bump it whenever
# content.py or the contract's layout changes: every contract then records
# which text it was issued under, and one issued under the old text reads as
# stale against a tenancy built with the new one.
TEMPLATE_VERSION = '2026-09-v1'

# The contract's own rule: a month is four weeks of every weekday rented, and a
# fifth in a long month costs nothing extra (content.SECTION_3_ITEMS, "מסגרת ונוהל התשלום").
WEEKS_PER_MONTH = 4

KIND_WEEKLY = 'weekly'
KIND_ONE_TIME = 'one_time'
PERIOD_MONTHLY = 'monthly'
PERIOD_ONCE = 'once'

_TWOPLACES = Decimal('0.01')


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f'{type(value).__name__} does not belong in contract terms')


def canonical_json(terms) -> str:
    """The one spelling of `terms` that is hashed: sorted keys, no spaces, Decimals as strings, UTF-8 as is."""
    return json.dumps(
        terms, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=_json_default,
    )


def terms_sha256(terms) -> str:
    """SHA-256, in hex, of the canonical JSON of `terms`."""
    return hashlib.sha256(canonical_json(terms).encode('utf-8')).hexdigest()


def money(value) -> str:
    """An amount as the terms hold it: a string to the agora, '1416.00'."""
    return str(Decimal(str(value or 0)).quantize(_TWOPLACES, rounding=ROUND_HALF_UP))


def clean_text(value) -> str:
    """Text on one line with single spaces: what the contract prints, however it was typed."""
    return ' '.join(str(value or '').split())


def iso_date(value) -> str | None:
    """A date as the terms hold it, 'YYYY-MM-DD', or None."""
    if value in (None, ''):
        return None
    return value.isoformat() if hasattr(value, 'isoformat') else str(value)


def _hhmm(value) -> str | None:
    """'HH:MM' from a time or an 'HH:MM[:SS]' string, or None when there is no time."""
    if value in (None, ''):
        return None
    if isinstance(value, time_cls):
        return value.strftime('%H:%M')
    hours, _, rest = str(value).partition(':')
    return f'{int(hours):02d}:{int(rest.partition(":")[0] or 0):02d}'


def studio_details() -> dict:
    """The studio's side of the contract, from content.py."""
    return {
        'name': content.STUDIO_NAME,
        'company_number': content.STUDIO_COMPANY_NUMBER,
        'email': content.STUDIO_EMAIL,
        'phone': content.STUDIO_PHONE,
    }


def _row(kind, *, weekday, date, start, end, studio, rate, sessions) -> dict:
    return {
        'kind': kind,
        'weekday': weekday,
        'date': date,
        'start_time': _hhmm(start),
        'end_time': _hhmm(end),
        'studio': studio,
        'rate': money(rate),
        'sum': money(rate * sessions),
    }


def slot_rows(slot) -> list[dict]:
    """
    A calendar slot as rows of the payment table, read the way the calendar reads it.

    A weekly slot is one row per weekday it repeats on, at that day's hours
    (studio_conflict.event_day_time_pairs: the per-day hours when set, else the
    slot's own, and the weekday of its date when no weekday is listed), each
    billed rate × 4. A one-time rental is one row on its date, billed once.
    """
    rate = Decimal(str(slot.price_per_session or 0))
    studio = clean_text(slot.studio.name) if slot.studio_id else None
    if slot.event_type == KIND_WEEKLY:
        return [
            _row(
                KIND_WEEKLY, weekday=dow, date=None, start=start, end=end,
                studio=studio, rate=rate, sessions=WEEKS_PER_MONTH,
            )
            for dow, start, end in event_day_time_pairs(slot)
        ]
    return [
        _row(
            KIND_ONE_TIME, weekday=None, date=iso_date(slot.event_date), start=slot.start_time,
            end=slot.end_time, studio=studio, rate=rate, sessions=1,
        )
    ]


def row_order(row) -> tuple:
    """Weekly rows Sunday to Saturday, then one-time rentals by date; then by hours and studio."""
    return (
        0 if row['kind'] == KIND_WEEKLY else 1,
        row['weekday'] if row['weekday'] is not None else 0,
        row['date'] or '',
        row['start_time'] or '',
        row['end_time'] or '',
        row['studio'] or '',
        Decimal(row['rate']),
    )


def sort_rows(rows) -> list[dict]:
    """
    The rows in the contract's order. Two rows that tie on every key are the
    same row (the sum follows from the kind and the rate), so the order never
    depends on the order the database returned the slots in.
    """
    return sorted(rows, key=row_order)


def amounts(amount_before_vat) -> dict:
    """
    The amount before VAT, the VAT and the total, each to the agora.

    The total is apps.core.vat.add_vat — what Tenancy.monthly_total shows and
    what will be charged — and the VAT is the difference, so the three always add up.
    """
    net = Decimal(money(amount_before_vat))
    total = add_vat(net)
    return {
        'monthly_amount': money(net),
        'vat_rate': str(VAT_RATE),
        'vat_amount': money(total - net),
        'monthly_total': money(total),
    }


def event_terms(event) -> dict:
    """
    The terms of the calendar's per-event download (scheduling/events/{id}/rental_agreement/).

    One studio-rental event, as that PDF has always shown it: the renter's name
    and ID from the event, and the rows' own sum as the amount, since an event
    has no agreed amount of its own. Nothing is stored. A tenancy's contracts
    are built by apps.rentals.contracts.build_terms instead.
    """
    if not event.is_studio_rental:
        raise ValueError('האירוע אינו שכירות סטודיו')
    if not event.contract_start_date or not event.contract_end_date:
        raise ValueError('חסרים תאריכי תוקף הסכם')
    rows = slot_rows(event)
    return {
        'template_version': TEMPLATE_VERSION,
        'studio': studio_details(),
        'tenant': {
            'name': clean_text(event.renter_name),
            'company_number': '',
            'id_number': clean_text(event.renter_id_number),
            'phone': '',
            'email': '',
            'address': '',
        },
        'branch': {'name': clean_text(event.branch.name)} if event.branch_id else None,
        'activity': clean_text(event.name),
        'rows': rows,
        'period': PERIOD_MONTHLY if event.event_type == KIND_WEEKLY else PERIOD_ONCE,
        **amounts(sum((Decimal(row['sum']) for row in rows), Decimal('0'))),
        'billing_day': None,
        'start_date': iso_date(event.contract_start_date),
        'end_date': iso_date(event.contract_end_date),
    }
