"""
One transaction, as Tranzila's report has it, beside what the system recorded.

Read only: the server asks the terminal's report (GET-like, /v1/transactions by
number) with the keys it already holds, and returns a short, safe summary —
never the card, the token or its expiry. It answers "did this really happen,
for this sum, with this approval, after this order?" without anyone logging in
to Tranzila, and is the base for the nightly two-way reconciliation.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from apps.core.tranzila_service import (
    TranzilaService,
    is_tranzila_approved,
    report_transaction_amount,
)

logger = logging.getLogger(__name__)

# The report's transaction_date / transaction_time are Israel local time
# ('2026-09-23' '16:30:30' for a 16:30 charge, 23.9.2026).
REPORT_TZ = ZoneInfo('Asia/Jerusalem')

# tranmodes that moved money to us: a charge, and a charge that made a token.
CHARGE_TRANMODES = frozenset({'A', 'AK'})

# A report row may predate the order by this much (clock skew), never more.
_SKEW = timedelta(minutes=10)

# The only report fields that ever leave the server. Card number, token,
# expiry and the payer's details stay out.
SAFE_REPORT_FIELDS = ('index', 'tranmode', 'processor_response_code', 'authorization_number',
                      'transaction_date', 'transaction_time', 'currency', 'txn_type', 'transtatus')


class CheckError(ValueError):
    """The request can't be checked (unknown terminal, bad number, no such order)."""


def _report_time(row: dict) -> Optional[datetime]:
    day = str(row.get('transaction_date') or '').strip()
    clock = str(row.get('transaction_time') or '').strip() or '00:00:00'
    try:
        return datetime.strptime(f'{day} {clock}', '%Y-%m-%d %H:%M:%S').replace(tzinfo=REPORT_TZ)
    except ValueError:
        return None


def _same_approval(reported, recorded) -> bool:
    left = str(reported or '').strip().lstrip('0')
    right = str(recorded or '').strip().lstrip('0')
    return bool(left) and left == right


def _our_record(terminal: str, index: str) -> Optional[dict]:
    """The order of ours that holds this transaction number on this terminal, if any."""
    from apps.payment_links.models import PaymentLinkPayment
    from apps.store.models import StoreInvoice

    invoice = (
        StoreInvoice.objects.filter(tranzila_transaction_id=index, tranzila_terminal=terminal)
        .order_by('-created_at').first()
    )
    if invoice is not None:
        return _describe_invoice(invoice)
    link_payment = (
        PaymentLinkPayment.objects.filter(gateway_transaction_id=index)
        .select_related('link').order_by('-created_at').first()
    )
    if link_payment is not None:
        return {
            'kind': 'payment_link',
            'reference': link_payment.link.title,
            'amount': str(link_payment.amount),
            'status': link_payment.status,
            'approval_number': link_payment.gateway_confirmation_code,
            'created_at': link_payment.created_at.isoformat(),
            '_created': link_payment.created_at,
        }
    return None


def _describe_invoice(invoice) -> dict:
    return {
        'kind': 'store_invoice',
        'reference': invoice.invoice_number,
        'amount': str(invoice.total_amount),
        'status': invoice.payment_status,
        'approval_number': invoice.tranzila_confirmation_code,
        'created_at': invoice.created_at.isoformat(),
        '_created': invoice.created_at,
    }


def check_transaction(*, invoice_number: str = '', terminal: str = '', index: str = '') -> dict:
    """
    Look one transaction up on its terminal and compare it with our record.

    Either `invoice_number` (a store invoice: its terminal and number are on
    it), or `terminal` + `index`. Raises CheckError for a request that can't
    be checked; a Tranzila that can't be reached comes back as
    `{'tranzila': {'reachable': False, ...}}`, never as a guess.
    """
    ours = None
    if invoice_number:
        from apps.store.models import StoreInvoice

        invoice = StoreInvoice.objects.filter(invoice_number=invoice_number.strip()).first()
        if invoice is None:
            raise CheckError('לא נמצאה חשבונית במספר הזה')
        terminal = invoice.tranzila_terminal
        index = invoice.tranzila_transaction_id
        if not terminal or not index:
            raise CheckError('על החשבונית אין מסוף או מספר עסקה — היא לא שולמה בטרנזילה, או שקדמה לרישום המסוף')
        ours = _describe_invoice(invoice)

    terminal = (terminal or '').strip()
    index = str(index or '').strip()
    if not index.isdigit():
        raise CheckError('מספר עסקה חייב להיות מספר')
    service = TranzilaService.for_terminal(terminal)
    if service is None:
        raise CheckError(f'המסוף "{terminal}" אינו מוגדר במערכת')
    if ours is None:
        ours = _our_record(terminal, index)

    found = service.find_transaction(index)
    result = {'terminal': terminal, 'index': index, 'ours': None, 'comparison': []}
    if not found.get('success'):
        logger.warning('Tranzila check %s/%s: report unavailable: %s', terminal, index, found.get('error'))
        result['tranzila'] = {'reachable': False, 'error': str(found.get('error') or 'אין תשובה מטרנזילה')}
    else:
        row = found.get('transaction')
        if not row:
            result['tranzila'] = {'reachable': True, 'found': False}
        else:
            amount = report_transaction_amount(row)
            made_at = _report_time(row)
            result['tranzila'] = {
                'reachable': True,
                'found': True,
                'amount': str(amount) if amount is not None else None,
                'approved': is_tranzila_approved(row.get('processor_response_code')),
                'is_charge': str(row.get('tranmode') or '').strip().upper() in CHARGE_TRANMODES,
                'made_at': made_at.isoformat() if made_at else None,
                **{key: str(row.get(key) or '') for key in SAFE_REPORT_FIELDS},
            }
            if ours is not None:
                result['comparison'] = _compare(result['tranzila'], ours, amount, made_at)

    if ours is not None:
        result['ours'] = {key: value for key, value in ours.items() if not key.startswith('_')}
    return result


def _compare(tranzila: dict, ours: dict, amount: Optional[Decimal], made_at: Optional[datetime]) -> list:
    """Each thing that has to agree for the payment to be real, with both sides."""
    created = ours['_created']
    return [
        {'check': 'approved', 'label': 'העסקה אושרה בטרנזילה', 'ok': tranzila['approved'],
         'tranzila': tranzila['processor_response_code'], 'ours': ''},
        {'check': 'is_charge', 'label': 'חיוב אמיתי (לא בדיקת כרטיס או זיכוי)', 'ok': tranzila['is_charge'],
         'tranzila': tranzila['tranmode'], 'ours': ''},
        {'check': 'amount', 'label': 'אותו סכום', 'ok': amount is not None and amount == Decimal(ours['amount']),
         'tranzila': tranzila['amount'], 'ours': ours['amount']},
        {'check': 'approval_number', 'label': 'אותו מספר אישור',
         'ok': _same_approval(tranzila['authorization_number'], ours['approval_number']),
         'tranzila': tranzila['authorization_number'], 'ours': ours['approval_number']},
        {'check': 'after_order', 'label': 'העסקה נעשתה אחרי ההזמנה',
         'ok': made_at is not None and made_at >= created.replace(microsecond=0) - _SKEW,
         'tranzila': tranzila['made_at'], 'ours': ours['created_at']},
    ]

