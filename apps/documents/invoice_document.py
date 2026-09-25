"""The parts every customer-facing document says, whatever produced it.

The business block, the footer line and the statutory small print are the same
sentences on a lesson receipt, a store sale and a hand-issued invoice, so they
are written once here. ``invoice_layout`` draws them; this module decides what
they say.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from apps.documents.invoice_layout import Field, Note
from apps.documents.issuer import (
    ARCHIVE_MARK,
    COMPUTERIZED_MARK,
    COPY_MARK,
    ORIGINAL_MARK,
    SIGNED_MARK,
    ISSUER_ADDRESS,
    ISSUER_COMPANY_NUMBER,
    ISSUER_EMAIL,
    ISSUER_NAME,
    ISSUER_PHONE,
)

# Allocation number threshold (net, before VAT) — Israel Tax Authority.
#
# A setting rather than a constant because the figure steps down year by year
# and is decided outside this code. It is printed on real invoices, so getting
# it wrong is visible to customers: it must be changeable without a release.
ALLOCATION_THRESHOLD = Decimal(str(getattr(settings, 'ALLOCATION_THRESHOLD_ILS', '5000')))


def business_fields() -> list[Field]:
    """
    פרטי העסק, as תקנה 9א(א)(1) wants them: the words "עוסק מורשה", the
    registration number, the business's name and its address — printed on the
    face of the document, not carried by a letterhead graphic.
    """
    return [
        Field('שם העסק', ISSUER_NAME),
        Field('עוסק מורשה / ח.פ.', ISSUER_COMPANY_NUMBER),
        Field('כתובת', ISSUER_ADDRESS),
        Field('טלפון', ISSUER_PHONE),
    ]


def footer_line() -> str:
    return f'{ISSUER_NAME} - {ISSUER_ADDRESS}  |  {ISSUER_PHONE}  |  {ISSUER_EMAIL}'


def issue_stamp(moment) -> str:
    """A timestamp as the document shows it, or '' when the record has none."""
    if moment is None:
        return ''
    if isinstance(moment, datetime):
        try:
            moment = timezone.localtime(moment)
        except (ValueError, TypeError):       # a naive datetime on an old row
            pass
        return moment.strftime('%d/%m/%Y %H:%M')
    return date_stamp(moment)


def date_stamp(day) -> str:
    if day is None:
        return ''
    if isinstance(day, str):
        parts = day[:10].split('-')
        return f'{parts[2]}/{parts[1]}/{parts[0]}' if len(parts) == 3 else day
    if isinstance(day, (date, datetime)):
        return day.strftime('%d/%m/%Y')
    return str(day)


def allocation_required(net_before_vat) -> bool:
    """
    Whether the amount is above the threshold. סעיף 38(א1) לחוק מע"מ speaks of a
    tax invoice "שסכומה, בלא המס, עולה על" the threshold — so an invoice of
    exactly ₪5,000 before VAT needs none.
    """
    return Decimal(str(net_before_vat or 0)) > ALLOCATION_THRESHOLD


# The documents an allocation number is asked for (הוראת ביצוע מע"מ 01/2025;
# the Tax Authority's FAQ: a credit note needs none).
ALLOCATION_DOCUMENT_TYPES = ('tax_invoice', 'combined')


def document_needs_allocation(document_type: str, net_before_vat, *, to_business: bool) -> bool:
    """
    A tax invoice (or invoice-receipt) above the threshold, to a business customer.

    The number is what lets an עוסק מורשה deduct the input VAT; a private family
    deducts nothing, and a credit note is issued without one.
    """
    return (
        document_type in ALLOCATION_DOCUMENT_TYPES
        and to_business
        and allocation_required(net_before_vat)
    )


def _threshold_label() -> str:
    value = ALLOCATION_THRESHOLD.quantize(Decimal('1'))
    return f'{value:,}'


def allocation_note(net_before_vat: Decimal, allocation_number: str = '', *,
                    to_business: bool = True, credit: bool = False) -> Note | None:
    """
    The allocation-number line on the document.

    Three states, and the reader is told which one they are looking at: the
    number itself once it has been entered, that one is needed and is still
    missing, or that this transaction needs none. A credit note says nothing
    unless a number was entered for it; a private customer above the threshold
    is told none is needed, since only an עוסק מורשה deducts input VAT.
    """
    number = (allocation_number or '').strip()
    if number:
        return Note('מספר הקצאה:', number)
    if credit:
        return None
    if allocation_required(net_before_vat):
        if not to_business:
            return Note('מספר הקצאה:', 'לא נדרש לעסקה זו - הלקוח אינו עוסק מורשה.')
        return Note(
            'מספר הקצאה:',
            f'נדרש לעסקה זו (סכום לפני מע"מ מעל {_threshold_label()} ₪) — טרם הוזן.',
        )
    return Note(
        'מספר הקצאה:',
        f'לא נדרש לעסקה זו - סכום העסקה לפני מע"מ אינו עולה על {_threshold_label()} ₪.',
    )


def computerized_note() -> Note:
    """סעיף 18ב(א): a document sent by computer says so, בצורה בולטת לעין."""
    return Note(f'{COMPUTERIZED_MARK}:', 'מסמך זה הופק באופן דיגיטלי.')


def signature_note() -> Note:
    """The signed original's line: it is signed, and how. Never on a copy — a copy is not signed."""
    return Note('חתימה אלקטרונית:', f'{SIGNED_MARK}.')


def archive_note(on: date | None = None) -> Note:
    """
    The archive copy's line: when it was drawn again, and that it is not what the customer got.

    A document issued before signing existed is drawn again from its record to
    be signed and kept (apps/documents/signing/archive.py). The page must say
    so in words — an accountant or an assessor reading it later should not have
    to know the signature's date to tell it from the original.
    """
    day = on or timezone.localdate()
    return Note(
        f'{ARCHIVE_MARK}:',
        f'הופק מחדש מנתוני המסמך ביום {date_stamp(day)} ונחתם לשמירה בארכיון. אינו המקור שנמסר ללקוח.',
    )


@dataclass(frozen=True)
class Edition:
    """Which print of a document is being drawn: what its head says, its closing lines, and its seal."""
    copy_mark: str
    closing_notes: tuple[Note, ...] = ()
    seal: bool = False
    seal_centre_text: str = ''


def edition(*, copy: bool = False, signed: bool = False, archive: bool = False) -> Edition:
    """
    The one print the three generators draw for (copy, signed, archive) — never two at once.

    - ``archive``: the signed copy of a document issued before signing existed,
      kept for the business's own archive. It wins over the other two: its
      customer already holds the "מקור", and the software must not produce one
      twice (תקנה 9א(א)(2), הוראה 18(ב)(2)) — so "העתק לארכיון", the signature
      line, the archive note, and the seal with "העתק לארכיון" in its centre.
    - ``signed`` and not ``copy``: the original signed at issue — "מקור", the
      signature line and the seal.
    - otherwise: "העתק" for an office copy, "מקור" for an unsigned original
      (signing off, as before) — neither signed, so no line and no seal.
    """
    if archive:
        return Edition(ARCHIVE_MARK, (signature_note(), archive_note()), True, ARCHIVE_MARK)
    if copy:
        return Edition(COPY_MARK)
    if signed:
        return Edition(ORIGINAL_MARK, (signature_note(),), True)
    return Edition(ORIGINAL_MARK)


def late_note(issued_at, received_at) -> Note | None:
    """
    A receipt issued after the money came in says so on its face, with both
    dates — the one thing an accountant needs to place it in the right period.
    """
    issued = date_stamp(issued_at)
    received = date_stamp(received_at)
    if not issued and not received:
        return None
    return Note(
        'הופק באיחור:',
        f'המסמך הופק ביום {issued}; התשלום התקבל ביום {received}.',
    )


def credit_reference_note(document_number: str, document_date, reason: str) -> Note | None:
    """סעיף 9(ה): a credit note names the document it credits, its date and why."""
    parts = []
    if document_number:
        parts.append(f'מסמך מקורי {document_number}')
    stamp = date_stamp(document_date)
    if stamp:
        parts.append(f'מיום {stamp}')
    if reason:
        parts.append(f'סיבת הזיכוי: {reason}')
    if not parts:
        return None
    return Note('זיכוי עבור:', ' · '.join(parts) + '.')
