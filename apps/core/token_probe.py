"""
A 1 ₪ test of the course path before it is built: can a card saved on
Tranzila's hosted page (cogolive, tranmode NK — card check plus token, no
charge) be charged by our server on the token terminal (cogolivetok)?

Tranzila was asked (25.9.2026) and has not answered; this answers it with the
owner's own card, in four steps a manager runs:

  1. open_page  — the NK page on TRANZILA_TERMINAL for 1 ₪. Nothing is charged.
  2. find_rows  — the page's row in that terminal's report: whether a token and
                  an expiry came back. The token itself never leaves the server.
  3. charge     — 1 ₪ from that token on a terminal of the hosted pair. Once per
                  row and terminal; a real charge, run only when the owner says so.
  4. refund     — that 1 ₪ back, once.

Every attempt is kept as a tranzila_transactions row keyed token_probe_…, so
the result stays on record and a second press charges nothing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.tranzila_service import (
    TranzilaService,
    is_tranzila_approved,
    is_tranzila_uncertain_gateway_error,
    report_transaction_amount,
)

logger = logging.getLogger(__name__)

PROBE_SUM = Decimal('1.00')
PROBE_DESCRIPTION = 'בדיקת כרטיס שמור'
# Page modes that make a token: J2 check (NK), J5 check (VK), token only (K).
PAGE_TRANMODES = ('NK', 'VK', 'K')
REPORT_TZ = ZoneInfo('Asia/Jerusalem')
_SKEW = timedelta(minutes=10)


class ProbeError(ValueError):
    """The step can't run as asked."""


def _hosted_pair() -> tuple[str, str]:
    return (
        (getattr(settings, 'TRANZILA_TERMINAL', '') or '').strip(),
        (getattr(settings, 'TRANZILA_TOKEN_TERMINAL', '') or '').strip(),
    )


def _report_time(row: dict) -> Optional[datetime]:
    day = str(row.get('transaction_date') or '').strip()
    clock = str(row.get('transaction_time') or '').strip() or '00:00:00'
    try:
        return datetime.strptime(f'{day} {clock}', '%Y-%m-%d %H:%M:%S').replace(tzinfo=REPORT_TZ)
    except ValueError:
        return None


def _describe(row: dict) -> dict:
    """What may be shown of a report row: never the card, the token or its expiry."""
    made_at = _report_time(row)
    amount = report_transaction_amount(row)
    return {
        'index': str(row.get('index') or row.get('transaction_index') or ''),
        'tranmode': str(row.get('tranmode') or ''),
        'approved': is_tranzila_approved(row.get('processor_response_code')),
        'processor_response_code': str(row.get('processor_response_code') or ''),
        'amount': str(amount) if amount is not None else None,
        'made_at': made_at.isoformat() if made_at else None,
        'has_token': bool(str(row.get('credit_card_token') or '').strip()),
        'has_expiry': bool(row.get('expiration_month') and row.get('expiration_year')),
    }


def open_page(*, tranmode: str = 'NK') -> dict:
    """Step 1: the hosted page on TRANZILA_TERMINAL, 1 ₪, in a mode that saves the card."""
    tranmode = (tranmode or 'NK').strip().upper()
    if tranmode not in PAGE_TRANMODES:
        raise ProbeError(f'מצב עמוד לא מוכר: {tranmode}')
    service = TranzilaService.iframe()
    if service.credential_error():
        raise ProbeError(f'למסוף {service.terminal} אין מפתחות: {service.credential_error()}')
    url = service.create_payment_request(
        amount=PROBE_SUM,
        description=PROBE_DESCRIPTION,
        tranmode=tranmode,
    )
    return {'url': url, 'terminal': service.terminal, 'tranmode': tranmode, 'opened_at': timezone.now().isoformat()}


def find_rows(*, since: Optional[datetime] = None) -> dict:
    """Step 2: today's rows on TRANZILA_TERMINAL that saved a card, newest first."""
    service = TranzilaService.iframe()
    day = timezone.localtime(timezone.now(), REPORT_TZ).date()
    response = service.list_all_transactions(day, day, max_pages=2)
    if not response.get('success'):
        return {'terminal': service.terminal, 'reachable': False, 'error': str(response.get('error') or '')}
    rows = []
    for row in response.get('transactions') or []:
        mode = str(row.get('tranmode') or '').strip().upper()
        if not mode.endswith('K') or mode == 'AK':
            continue
        made_at = _report_time(row)
        if since is not None and made_at is not None and made_at < since - _SKEW:
            continue
        rows.append(_describe(row))
    rows.sort(key=lambda r: r['made_at'] or '', reverse=True)
    return {'terminal': service.terminal, 'reachable': True, 'rows': rows}


def _page_row(index: str) -> dict:
    index = str(index or '').strip()
    if not index.isdigit():
        raise ProbeError('מספר עסקה חייב להיות מספר')
    found = TranzilaService.iframe().find_transaction(index)
    if not found.get('success'):
        raise ProbeError(f"טרנזילה לא ענתה: {found.get('error') or ''}")
    row = found.get('transaction')
    if not row:
        raise ProbeError('העסקה לא נמצאה בדוח של המסוף')
    mode = str(row.get('tranmode') or '').strip().upper()
    if not mode.endswith('K') or mode == 'AK':
        raise ProbeError(f'העסקה הזאת לא נוצרה בעמוד ששומר כרטיס (tranmode {mode})')
    if not is_tranzila_approved(row.get('processor_response_code')):
        raise ProbeError('העסקה לא אושרה')
    if not str(row.get('credit_card_token') or '').strip():
        raise ProbeError('בדוח אין טוקן לעסקה הזאת')
    if not (row.get('expiration_month') and row.get('expiration_year')):
        raise ProbeError('בדוח אין תוקף לעסקה הזאת')
    return row


def _charge_key(index: str, terminal: str) -> str:
    return f'token_probe_charge_{index}_{terminal}'


def charge(*, index: str, terminal: str) -> dict:
    """Step 3: 1 ₪ from the page's token on `terminal` (TRANZILA_TOKEN_TERMINAL or TRANZILA_TERMINAL)."""
    from apps.customers.models import TranzilaTransaction

    terminal = (terminal or '').strip()
    if not terminal or terminal not in _hosted_pair():
        raise ProbeError('אפשר לחייב רק במסופים של עמוד התשלום (cogolive / cogolivetok)')
    row = _page_row(index)
    index = str(index).strip()
    client = TranzilaService.for_terminal(terminal)
    if client is None:
        raise ProbeError(f'המסוף {terminal} אינו מוגדר במערכת')

    try:
        with transaction.atomic():
            claim = TranzilaTransaction.objects.create(
                transaction_id='', confirmation_code='', transaction_type='charge',
                response_code='', response_message='', response_data={},
                request_data={'probe': 'charge', 'page_index': index, 'terminal': terminal,
                              'page_tranmode': str(row.get('tranmode') or ''), 'amount': str(PROBE_SUM)},
                idempotency_key=_charge_key(index, terminal), is_successful=False,
            )
    except IntegrityError:
        raise ProbeError('כבר ניסינו לחייב את הכרטיס הזה במסוף הזה. התוצאה שמורה — אין חיוב נוסף.')

    result = client.charge_with_token(
        token=str(row.get('credit_card_token')).strip(),
        amount=PROBE_SUM,
        description=PROBE_DESCRIPTION,
        expire_month=int(row.get('expiration_month')),
        expire_year=int(row.get('expiration_year')),
        duplicate_guard_key=f'probe-{index}-{terminal}',
    )
    uncertain = bool(result.get('uncertain')) or is_tranzila_uncertain_gateway_error(result)
    claim.transaction_id = str(result.get('transaction_id') or '')[:100]
    claim.confirmation_code = str(result.get('confirmation_code') or '')[:100]
    claim.response_code = str(result.get('response_code') or '')[:10]
    claim.response_message = str(result.get('error') or result.get('message') or '')[:1000]
    claim.response_data = result.get('raw_response') or {}
    claim.is_successful = bool(result.get('success'))
    claim.response_timestamp = timezone.now()
    claim.save()
    outcome = 'charged' if result.get('success') else ('uncertain' if uncertain else 'refused')
    logger.info('Token probe: page %s charged on %s -> %s (%s)', index, terminal, outcome, claim.response_code)
    return {
        'outcome': outcome,
        'terminal': terminal,
        'page_index': index,
        'transaction_id': claim.transaction_id,
        'confirmation_code': claim.confirmation_code,
        'response_code': claim.response_code,
        'message': claim.response_message,
    }


def refund(*, index: str, terminal: str) -> dict:
    """Step 4: the probe's 1 ₪ back, once."""
    from apps.customers.models import TranzilaTransaction

    index = str(index or '').strip()
    terminal = (terminal or '').strip()
    charged = TranzilaTransaction.objects.filter(
        idempotency_key=_charge_key(index, terminal), is_successful=True,
    ).first()
    if charged is None or not charged.transaction_id:
        raise ProbeError('לא נמצא חיוב בדיקה מוצלח לעסקה הזאת במסוף הזה')
    row = _page_row(index)
    client = TranzilaService.for_terminal(terminal)
    if client is None:
        raise ProbeError(f'המסוף {terminal} אינו מוגדר במערכת')
    try:
        with transaction.atomic():
            claim = TranzilaTransaction.objects.create(
                transaction_id='', confirmation_code='', transaction_type='refund',
                response_code='', response_message='', response_data={},
                request_data={'probe': 'refund', 'page_index': index, 'terminal': terminal,
                              'original_transaction_id': charged.transaction_id, 'amount': str(PROBE_SUM)},
                idempotency_key=f'token_probe_refund_{index}_{terminal}', is_successful=False,
            )
    except IntegrityError:
        raise ProbeError('כבר ביקשנו זיכוי לחיוב הבדיקה הזה. אין זיכוי נוסף.')

    # A credit, not a same-day cancel: a credit refused as "not settled yet"
    # falls back to a cancel only after Tranzila has answered no.
    result = client.refund_transaction(
        transaction_id=charged.transaction_id,
        authorization_number=charged.confirmation_code,
        card_expire_month=int(row.get('expiration_month')),
        card_expire_year=int(row.get('expiration_year')),
        token=str(row.get('credit_card_token')).strip(),
        amount=PROBE_SUM,
        reason=PROBE_DESCRIPTION,
        prefer_cancel=False,
        terminal_name=terminal,
    )
    uncertain = bool(result.get('uncertain')) or is_tranzila_uncertain_gateway_error(result)
    if not result.get('success') and not uncertain:
        # Tranzila answered no: nothing came back to the card, a retry is safe.
        claim.delete()
        logger.info('Token probe: refund of %s on %s refused: %s', charged.transaction_id, terminal, result.get('error'))
        return {
            'refunded': False, 'uncertain': False, 'terminal': terminal,
            'original_transaction_id': charged.transaction_id,
            'response_code': str(result.get('response_code') or ''),
            'message': str(result.get('error') or result.get('message') or ''),
        }
    claim.transaction_id = str(result.get('transaction_id') or '')[:100]
    claim.confirmation_code = str(result.get('confirmation_code') or '')[:100]
    claim.response_code = str(result.get('response_code') or '')[:10]
    claim.response_message = str(result.get('error') or result.get('message') or '')[:1000]
    claim.response_data = result.get('raw_response') or {}
    claim.is_successful = bool(result.get('success'))
    claim.response_timestamp = timezone.now()
    claim.save()
    logger.info('Token probe: refund of %s on %s -> %s', charged.transaction_id, terminal, result.get('success'))
    return {
        'refunded': bool(result.get('success')),
        'uncertain': uncertain,
        'terminal': terminal,
        'original_transaction_id': charged.transaction_id,
        'transaction_id': claim.transaction_id,
        'response_code': claim.response_code,
        'message': claim.response_message,
    }
