"""Charges that never got their חשבונית מס / קבלה — found, listed and issued late.

Every path that takes money issues a receipt, but each of those calls is
non-fatal on purpose (a mail or PDF failure must not undo a charge that went
through), so a charge can end up without one. `check_invoices` finds them from a
terminal; the missing-receipts screen finds them for the office. Both read the
definition and the late issue from here, so the list the accountant approves is
the list that gets issued, and a receipt issued from the screen is exactly the
one `check_invoices --fix` would have issued:

  * dated TODAY, the day it is produced — dating it back would put an earlier
    date on a higher number than documents already issued;
  * issued in the order the money arrived, and marked "הופק באיחור" with both
    dates (the `issued_late` activity log, which the PDF prints);
  * not mailed — the screen never mails; only the command's --email does.

A charge has its receipt when an Invoice points at it, or when a family
checkout's receipt names it in its checkout_lines log (one receipt for every
child and lesson paid together — checkout_invoice.py).
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.documents.numbering import SERIES_SUBSCRIPTION

logger = logging.getLogger(__name__)

# What the office types to issue. A button alone is one click away from
# numbers in the IR run that can never be taken back.
CONFIRM_WORD = 'הפק'

# One request stays well inside a serverless timeout; a longer backlog is
# issued in a few rounds, each one idempotent.
MAX_ISSUE_BATCH = 100

# A charge completed this recently is not "missing" yet — not on the list, and
# not at the moment of issue. A family checkout marks its charges completed,
# issues its one receipt pointing at the first, and only then writes the log
# naming the others (checkout_invoice._finish_checkout_invoice); until it does,
# those others look receipt-less, and a late receipt issued then would be a
# second one. Every path that completes a charge stamps payment_date as it
# does, so that date is when it was completed. A real miss is still missing
# fifteen minutes later.
RECENT_CHARGE_GRACE = timedelta(minutes=15)

# A document issued by hand this close to the charge, for the same money and
# the same family, may be the receipt the charge is missing.
MANUAL_DOCUMENT_WINDOW = timedelta(days=45)

CHANNEL_LABELS = {
    'trial': 'שיעור ניסיון',
    'registration': 'הרשמה / דמי רישום',
    'card_link': 'קישור לתשלום',
    'standing_order': 'הוראת קבע',
    'one_time': 'חד-פעמי',
}

# What the receipt itself will say: _create_invoice_from_payment records a card
# when the charge went through Tranzila, and nothing otherwise.
METHOD_LABELS = {
    'credit_card': 'אשראי',
    '': 'לא רשום',
}

SKIP_MESSAGES = {
    'not_found': 'התשלום לא נמצא',
    'not_completed': 'החיוב לא הושלם, או שאין בו סכום',
    'has_receipt': 'כבר הופקה לו קבלה',
    'in_checkout_receipt': 'כבר כלול בקבלה של רכישה משותפת',
    'too_recent': 'החיוב הושלם לפני פחות מ-15 דקות, וייתכן שהקבלה שלו עוד בהפקה',
}

# What the office sees for a receipt that failed. The exception itself can
# carry internals, so it goes to the server log and nowhere else.
FAILED_MESSAGE = 'ההפקה נכשלה — נסו שוב או פנו לתמיכה'


def charge_date(payment):
    """When the money came in: the charge's own date, or the row's when it has none."""
    return payment.payment_date or payment.created_at


def _completed_since(cutoff) -> Q:
    return Q(payment_date__gte=cutoff) | Q(payment_date__isnull=True, created_at__gte=cutoff)


def _is_recent(charged_on, now) -> bool:
    return charged_on is not None and charged_on >= now - RECENT_CHARGE_GRACE


def _missing_queryset(now=None):
    """
    Completed, actually charged, no Invoice pointing at it, and not completed in
    the last few minutes. The database's half of "missing" — payments_without_invoice
    adds the checkout receipts' logs.
    """
    from apps.customers.models import Payment

    now = now or timezone.now()
    return (
        Payment.objects
        .filter(status='completed', final_amount__gt=0, invoices__isnull=True)
        .exclude(_completed_since(now - RECENT_CHARGE_GRACE))
        .select_related('child', 'family', 'parent', 'branch', 'lesson__course',
                        'tranzila_transaction', 'card_link')
    )


def payments_without_invoice(since=None, *, year: int | None = None) -> list:
    """
    Every charge missing its receipt — oldest money first. The one definition of "missing".

    `since` is the command's window (rows created since then); `year` is the
    screen's, by the day the money came in.
    """
    from apps.customers.checkout_invoice import payments_covered_by_checkout

    rows = _missing_queryset()
    if since is not None:
        rows = rows.filter(created_at__gte=since)
    if year is not None:
        rows = rows.filter(
            Q(payment_date__year=year) | Q(payment_date__isnull=True, created_at__year=year)
        )
    rows = list(rows)
    covered = payments_covered_by_checkout(payment.id for payment in rows)
    return sorted((payment for payment in rows if str(payment.id) not in covered), key=charge_date)


# ------------------------------------------------------------------ the report

def _channel(payment) -> str:
    """Where the charge came from, in the payments tab's words."""
    if payment.trial_lesson_date:
        return 'trial'
    if payment.registration_fee and payment.registration_fee > 0:
        return 'registration'
    if hasattr(payment, 'card_link'):  # reverse one-to-one: absent raises, hasattr says no
        return 'card_link'
    if payment.payment_type == 'recurring_subscription':
        return 'standing_order'
    return 'one_time'


def _method(payment) -> str:
    return 'credit_card' if payment.tranzila_transaction_id else ''


def _description(payment) -> str:
    if payment.description:
        return payment.description
    lesson = payment.lesson if payment.lesson_id else None
    course = lesson.course if lesson is not None and lesson.course_id else None
    return course.name if course is not None else ''


def possible_manual_documents(payments) -> dict[str, dict]:
    """
    payment id -> a document issued by hand that may already be the charge's receipt.

    Before lesson charges issued receipts of their own the office covered some
    by hand, and nothing links a charge to such a document. So this matches the
    way the register's possible duplicates do (register.find_possible_duplicates,
    undocumented_income._issued_document_index): only documents issued for a
    registered child, never a draft or a credit, never a store sale's Tranzila
    copy, for the same sum. Here the child may be any child of the family —
    the office often issues one document to the family — and the date within
    45 days of the charge; the nearest one is named.

    A document is not used up by the first charge it matches: a flag only leaves
    the row unticked for the office to decide, while a missed one issues a
    second receipt for the same money.
    """
    from apps.documents.models import FormalDocument

    payments = list(payments)
    child_ids = {payment.child_id for payment in payments if payment.child_id}
    family_ids = {payment.family_id for payment in payments if payment.family_id}
    if not child_ids and not family_ids:
        return {}
    days = {str(payment.id): timezone.localtime(charge_date(payment)).date() for payment in payments}

    documents = (
        FormalDocument.objects
        .filter(child__isnull=False, store_invoices__isnull=True)
        .exclude(document_type__in=('draft', 'credit_invoice'))
        .filter(Q(child_id__in=child_ids) | Q(child__family_id__in=family_ids))
        .filter(total_amount__in={payment.final_amount for payment in payments})
        .filter(
            document_date__gte=min(days.values()) - MANUAL_DOCUMENT_WINDOW,
            document_date__lte=max(days.values()) + MANUAL_DOCUMENT_WINDOW,
        )
        .values_list('document_number', 'document_date', 'total_amount', 'child_id', 'child__family_id')
        .order_by('document_date', 'document_number')
    )
    by_amount: dict = {}
    for number, day, total, child_id, family_id in documents:
        by_amount.setdefault(Decimal(total), []).append((number, day, Decimal(total), child_id, family_id))

    flagged = {}
    for payment in payments:
        paid_on = days[str(payment.id)]
        nearest = None
        for number, day, total, child_id, family_id in by_amount.get(Decimal(payment.final_amount), ()):
            same_child = bool(payment.child_id) and child_id == payment.child_id
            same_family = bool(payment.family_id) and family_id == payment.family_id
            gap = abs((day - paid_on).days)
            if not (same_child or same_family) or gap > MANUAL_DOCUMENT_WINDOW.days:
                continue
            if nearest is None or gap < nearest[0]:
                nearest = (gap, number, day, total)
        if nearest is not None:
            _gap, number, day, total = nearest
            flagged[str(payment.id)] = {'number': number, 'date': day.isoformat(), 'amount': f'{total:.2f}'}
    return flagged


def receipt_row(payment, manual_document: dict | None = None) -> dict:
    channel, method = _channel(payment), _method(payment)
    return {
        'payment_id': str(payment.id),
        'paid_at': timezone.localtime(charge_date(payment)).isoformat(),
        'family_name': payment.family.name if payment.family_id else '',
        'child_name': payment.child.full_name if payment.child_id else '',
        'description': _description(payment),
        'amount': f'{payment.final_amount:.2f}',
        'channel': channel,
        'channel_label': CHANNEL_LABELS[channel],
        'method': method,
        'method_label': METHOD_LABELS[method],
        # {number, date, amount} of a document issued by hand that may already
        # cover this charge, or None.
        'possible_manual_document': manual_document,
    }


def _run_dict(run) -> dict:
    return {
        'series': run.series,
        'year': run.year,
        'name': run.name,
        'label': run.label,
        'issued': run.issued,
        'first': run.first,
        'last': run.last,
        'missing': list(run.missing),
        'complete': run.complete,
    }


def next_receipt_number() -> str:
    """The number the next late receipt would take — dated today, so from this year's IR run."""
    from apps.documents.models import DocumentSeries

    year = timezone.localdate().year
    row = DocumentSeries.objects.filter(series=SERIES_SUBSCRIPTION, year=year).first()
    return f'{SERIES_SUBSCRIPTION}-{year}-{(row.counter if row else 0) + 1:06d}'


def missing_receipts_report(year: int) -> dict:
    """The charges of a year missing their receipt, their total, and every run of that year checked for gaps."""
    from apps.documents.numbering import continuity

    payments = payments_without_invoice(year=year)
    manual = possible_manual_documents(payments)
    total = sum((payment.final_amount for payment in payments), Decimal('0.00'))
    return {
        'year': year,
        'count': len(payments),
        'total': f'{total:.2f}',
        'next_number': next_receipt_number(),
        'rows': [receipt_row(payment, manual.get(str(payment.id))) for payment in payments],
        'continuity': [_run_dict(run) for run in continuity(year)],
    }


CSV_COLUMNS = (
    'תאריך התשלום', 'משפחה', 'ילד', 'תיאור', 'סכום', 'ערוץ', 'אמצעי תשלום', 'מזהה תשלום',
    'ייתכן שכבר הופק ידנית',
)


def _day_text(iso: str) -> str:
    return f'{iso[8:10]}/{iso[5:7]}/{iso[0:4]}'


def missing_receipts_csv(report: dict) -> bytes:
    """
    The list for the accountant to approve, as a CSV that opens in Excel.

    Written the way the documents register is (register.register_csv): UTF-8
    with a byte-order mark so Excel reads the Hebrew, and any cell that starts
    like a formula quoted as text.
    """
    from apps.documents.register import _text

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\r\n')
    writer.writerow(CSV_COLUMNS)
    for row in report['rows']:
        manual = row.get('possible_manual_document')
        writer.writerow([
            _day_text(row['paid_at'][:10]),
            _text(row['family_name']),
            _text(row['child_name']),
            _text(row['description']),
            row['amount'],
            row['channel_label'],
            row['method_label'],
            _text(row['payment_id']),
            _text(f"{manual['number']} ({_day_text(manual['date'])})") if manual else '',
        ])
    return buffer.getvalue().encode('utf-8-sig')


# ------------------------------------------------------------------ the issue

def issue_late_receipt(payment, *, now, send_email: bool = False, backdate: bool = False, issued_by=None):
    """
    Issue one late receipt, or return None when the charge no longer needs one.

    The charge is locked and looked at again first: two clicks, or the screen
    and the command at once, must not give one payment two receipts — and
    neither may a family checkout's receipt that already names it, or one still
    being written for a charge completed a moment ago. The receipt and its
    "issued late" mark commit together, so no receipt ever stands without the
    dates it is printed with.
    """
    from apps.core.payment_service import PaymentService
    from apps.customers.checkout_invoice import payments_covered_by_checkout
    from apps.customers.financial_models import Invoice, InvoiceActivityLog
    from apps.customers.models import Payment

    charged_on = charge_date(payment)
    with transaction.atomic():
        state = (
            Payment.objects.select_for_update()
            .filter(pk=payment.pk)
            .values_list('status', 'final_amount', 'payment_date', 'created_at')
            .first()
        )
        if state is None or state[0] != 'completed' or not state[1] > 0:
            return None
        if _is_recent(state[2] or state[3], timezone.now()):
            return None
        if Invoice.objects.filter(payment_id=payment.pk).exists():
            return None
        if payments_covered_by_checkout([payment.pk]):
            return None

        invoice = PaymentService()._create_invoice_from_payment(
            payment,
            payment.tranzila_transaction,
            send_email=send_email,
            invoice_date=charged_on if backdate else now,
        )
        details = {
            'payment_id': str(payment.id),
            'money_received_at': charged_on.isoformat(),
            'document_issued_at': now.isoformat(),
            'backdated': backdate,
            'emailed': send_email,
        }
        if issued_by is not None:
            # Who pressed the button. The command's own runs carry no user and
            # keep the details they always had.
            details.update({
                'issued_by_user_id': str(issued_by.pk),
                'issued_by': issued_by.get_username(),
                'source': 'missing_receipts_screen',
            })
        InvoiceActivityLog.objects.create(invoice=invoice, action='issued_late', details=details)
    return invoice


def issue_missing_receipts(payment_ids, *, user) -> dict:
    """
    Issue what `check_invoices --fix` issues, for the charges the office chose.

    Dated today, in the order the money came in, marked late, never mailed and
    never backdated. A charge that is not missing its receipt any more (or never
    was) is skipped and says why; one that fails is reported and the rest go on,
    as in the command.
    """
    from apps.customers.checkout_invoice import payments_covered_by_checkout
    from apps.customers.financial_models import Invoice
    from apps.customers.models import Payment

    wanted = list(dict.fromkeys(str(pid) for pid in payment_ids))
    found = {
        str(payment.id): payment
        for payment in (
            Payment.objects.filter(pk__in=wanted)
            .select_related('child', 'family', 'parent', 'branch', 'lesson__course', 'tranzila_transaction')
        )
    }
    with_receipt = {
        str(pid) for pid in Invoice.objects.filter(payment_id__in=wanted).values_list('payment_id', flat=True)
    }
    in_checkout = payments_covered_by_checkout(found)

    issued, skipped, failed, candidates = [], [], [], []

    def skip(pid, reason, detail=''):
        message = SKIP_MESSAGES[reason] + (f' ({detail})' if detail else '')
        skipped.append({'payment_id': pid, 'reason': reason, 'message': message})

    now = timezone.now()
    for pid in wanted:
        payment = found.get(pid)
        if payment is None:
            skip(pid, 'not_found')
        elif payment.status != 'completed' or not payment.final_amount > 0:
            skip(pid, 'not_completed')
        elif pid in with_receipt:
            skip(pid, 'has_receipt')
        elif pid in in_checkout:
            skip(pid, 'in_checkout_receipt', in_checkout[pid])
        elif _is_recent(charge_date(payment), now):
            skip(pid, 'too_recent')
        else:
            candidates.append(payment)

    for payment in sorted(candidates, key=charge_date):
        pid = str(payment.id)
        try:
            invoice = issue_late_receipt(payment, now=now, send_email=False, backdate=False, issued_by=user)
        except Exception:
            logger.exception('Late receipt failed for payment %s', pid)
            failed.append({'payment_id': pid, 'message': FAILED_MESSAGE})
            continue
        if invoice is None:
            skip(pid, 'has_receipt')
            continue
        issued.append({'payment_id': pid, 'number': invoice.invoice_number})
        logger.info('Late receipt %s issued for payment %s by %s', invoice.invoice_number, pid, user.get_username())

    return {'issued': issued, 'skipped': skipped, 'failed': failed}
