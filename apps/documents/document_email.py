"""
Mail a document the office issued — its signed original once, or a copy after that.

Until 25.9.2026 a document issued by hand (a tax invoice, a receipt, a combined
invoice-receipt, a transaction invoice), or by a cash or check plan, was signed
and kept but never sent: its row said 'none', and the customer never got the
original. The owner's decision D5 is that every original reaches its customer —
by mail when 18ב(ד) allows it, otherwise on paper — so these documents have a
mail exit of their own now, CHANNEL_FORMAL, next to the four that existed
(lesson receipt, store sale, credit note, rental receipt).

It works exactly like them (apps/core/credit_note_email.py,
apps/rental_billing/receipt_email.py): the signing service's claim_email hands
over the stored signed bytes — only when the decision is email, the file still
matches its SHA-256 and nobody sent it before — and the claim is reported back
as sent or failed, so the cron can try again and nothing goes twice. Resend when
it is configured, Django's mail otherwise.

A copy (`send_document_copy_email`) is what the office's "שלח שוב" sends once
the original is out, by mail or on paper: drawn again as "העתק" and never the
original's bytes, since "מקור" leaves once (נספח ה'(א)(4)). It works for every
kind of document, not only the ones issued by hand.
"""
from __future__ import annotations

import base64
import logging
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone
from django.utils.html import escape

from apps.core.resend_email import resend_configured, send_resend_email
from apps.documents.issuer import COMPUTERIZED_MARK, COPY_MARK, ISSUER_ADDRESS, ISSUER_LINE, ISSUER_PHONE

logger = logging.getLogger(__name__)


def email_configured() -> bool:
    if resend_configured():
        return True
    return bool((getattr(settings, 'EMAIL_HOST', '') or '').strip())


def _day(value) -> str:
    if not value:
        return ''
    if isinstance(value, datetime):
        value = timezone.localtime(value) if timezone.is_aware(value) else value
    if isinstance(value, (date, datetime)):
        return value.strftime('%d/%m/%Y')
    return str(value)


def build_document_email(*, label: str, number: str, customer_name: str, document_date=None,
                         total: Decimal | None = None, copy: bool = False) -> tuple[str, str, str]:
    """
    (subject, plain text, html) for one document: its type and number, its date and total.

    A copy says it is one, in the subject and in the body — the customer should
    never take the copy for a second original.
    """
    name = (customer_name or '').strip() or 'לקוח/ה'
    heading = f'{label} {number}'
    subject = f'{COPY_MARK} — {heading} — קוגומלו' if copy else f'{heading} — קוגומלו'
    issued = _day(document_date)
    total_line = f'₪{total:.2f}' if total is not None else ''
    intro = (
        f'מצורף {COPY_MARK} של המסמך. המקור נמסר לך קודם לכן.' if copy
        else 'מצורף המסמך שהופק עבורך.'
    )

    text = (
        f'שלום {name},\n\n'
        f'{intro}\n\n'
        f'{label}: {number}\n'
        + (f'תאריך: {issued}\n' if issued else '')
        + (f'סה"כ: {total_line}\n' if total_line else '')
        + '\nהמסמך מצורף למייל בקובץ PDF.\n\n'
        f'{ISSUER_LINE}\n{ISSUER_ADDRESS} · {ISSUER_PHONE}\n\n'
        f'{COMPUTERIZED_MARK}\n\n'
        'בברכה,\nצוות קוגומלו'
    )
    rows = [(label, number)]
    if issued:
        rows.append(('תאריך', issued))
    if total_line:
        rows.append(('סה"כ', total_line))
    rows_html = ''.join(
        f'<tr><td style="padding:6px 0;color:#666">{escape(key)}</td>'
        f'<td style="padding:6px 0;font-weight:bold" dir="auto">{escape(value)}</td></tr>'
        for key, value in rows
    )
    html = f'''
<div dir="rtl" style="font-family:Arial,sans-serif;color:#25326a;max-width:620px;margin:auto">
  <h2 style="color:#303094">{escape(heading)}{f" · {COPY_MARK}" if copy else ""}</h2>
  <p style="line-height:1.7">שלום <b>{escape(name)}</b>,<br>{escape(intro)}</p>
  <table style="width:100%;border-collapse:collapse;margin-top:12px">{rows_html}</table>
  <p style="color:#303094;font-weight:bold;margin-top:12px">המסמך מצורף למייל בקובץ PDF.</p>
  <p style="color:#666;margin-top:22px;line-height:1.8">{escape(ISSUER_LINE)}<br>{escape(ISSUER_ADDRESS)} · <span dir="ltr">{escape(ISSUER_PHONE)}</span></p>
  <p style="margin-top:16px;padding:8px 12px;background:#eef1f5;color:#303094;font-weight:bold;display:inline-block">{COMPUTERIZED_MARK}</p>
  <p style="color:#666;margin-top:18px">בברכה,<br>צוות קוגומלו</p>
</div>'''
    return subject, text, html


def _deliver(*, email: str, subject: str, text: str, html: str, filename: str, pdf: bytes) -> None:
    """Hand the mail to the provider — Resend, or Django's mail. Raises when it does not go."""
    if resend_configured():
        send_resend_email(
            to=[email], subject=subject, text=text, html=html,
            attachments=[{'filename': filename, 'content': base64.b64encode(pdf).decode('ascii')}],
        )
        return
    message = EmailMultiAlternatives(
        subject, text, getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@kogomalo.com'), [email],
    )
    message.attach_alternative(html, 'text/html')
    message.attach(filename, pdf, 'application/pdf')
    message.send(fail_silently=False)


def send_formal_document_email(row, *, email: str = '') -> bool:
    """
    Mail a FormalDocument's signed original (CHANNEL_FORMAL). True when it was sent now.

    `row` is its SignedOriginal. The address is `email` when the office typed
    one, else where the row says the mail was meant to go, else the customer's
    own (the business customer's, or the child's family's — read now, so an
    address added later is found). False, and nothing sent, when there is no
    address or no mail provider, or when claim_email refuses: the row then
    says why (paper, held, already sent).
    """
    from apps.documents.models import FormalDocument, SignedOriginal
    from apps.documents.signing.service import KIND_FORMAL, claim_email
    from apps.documents.signing.sources import source_for

    doc = FormalDocument.objects.select_related(
        'business_customer', 'child', 'child__family', 'linked_document',
    ).get(pk=row.source_id)
    source = source_for(KIND_FORMAL, doc)
    address = (email or row.email_to or source.default_email or '').strip()
    if not address:
        logger.info('Document %s not mailed: the customer has no e-mail', doc.document_number)
        return False
    if not email_configured():
        logger.warning('No e-mail provider — document %s not sent', doc.document_number)
        return False

    claim = claim_email(KIND_FORMAL, doc, channel=SignedOriginal.CHANNEL_FORMAL, email_to=address)
    if claim is None:
        return False
    subject, text, html = build_document_email(
        label=source.type_label, number=doc.document_number,
        customer_name=row.customer_name or source.customer_name,
        document_date=doc.document_date, total=doc.total_amount,
    )
    try:
        _deliver(email=address, subject=subject, text=text, html=html,
                 filename=f'{doc.document_number}.pdf', pdf=claim.pdf)
    except Exception as exc:
        claim.failed(exc)
        raise
    claim.sent()
    logger.info('Document %s e-mailed (its signed original)', doc.document_number)
    return True


def send_document_copy_email(row, source, *, email: str) -> bool:
    """
    Mail a copy — "העתק", drawn again now — of a document whose original already left.

    Any kind (a lesson receipt, a store sale, a document issued by hand).
    Never the stored original's bytes, and nothing on the row changes: the
    original was mailed or printed once, and that stays the record. False when
    there is no mail provider; raises when the provider refuses.
    """
    if not email_configured():
        logger.warning('No e-mail provider — a copy of %s was not sent', row.number)
        return False
    pdf = source.render_copy()
    subject, text, html = build_document_email(
        label=row.document_type_label or source.type_label, number=row.number,
        customer_name=row.customer_name or source.customer_name,
        document_date=row.document_date or source.document_date,
        total=row.total if row.total is not None else source.total, copy=True,
    )
    _deliver(email=email, subject=subject, text=text, html=html, filename=f'{row.number}.pdf', pdf=pdf)
    return True
