"""Credit-note (הודעת זיכוי) emails, sent to the customer when a payment is refunded.

The body carries the details סעיף 9(ה) להוראות מס הכנסה (ניהול פנקסי חשבונות)
requires of a credit note: issuer name/address/registration number, date, customer
name, the number and date of the invoice being credited, the reason, the amount
before VAT, the VAT and its rate, and the total credited.

It goes out as a מסמך ממוחשב under סעיף 18ב, so the words appear on the document
itself — which is also what removes the signature requirement of סעיף 9(ה)(9).

Note for whoever wires the next caller: this mails the customer their copy. It is
not, on its own, the confirmed delivery that סעיף 23א(3) requires before the VAT
liability may be reduced.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from apps.core.resend_email import resend_configured, send_resend_email
from apps.core.vat import VAT_PERCENT_DISPLAY, split_vat_inclusive
from apps.documents.issuer import (
    COMPUTERIZED_MARK, ISSUER_ADDRESS, ISSUER_COMPANY_NUMBER, ISSUER_NAME, ISSUER_PHONE,
)

logger = logging.getLogger(__name__)

DOCUMENT_TITLE = 'הודעת זיכוי'


@dataclass(frozen=True)
class CreditNote:
    """One credit note, ready to be rendered into an email."""

    customer_name: str
    email: str
    amount: Decimal
    reason: str
    original_number: str = ''
    original_date: date | datetime | None = None
    document_number: str = ''
    issued_at: date | datetime | None = None

    @property
    def credited_on(self) -> date | datetime:
        return self.issued_at or timezone.now()


def _email_configured() -> bool:
    if resend_configured():
        return True
    return bool((getattr(settings, 'EMAIL_HOST', '') or '').strip())


def _fmt_date(value: date | datetime | None) -> str:
    if not value:
        return ''
    if isinstance(value, datetime):
        value = timezone.localtime(value) if timezone.is_aware(value) else value
    return value.strftime('%d/%m/%Y')


def build_credit_note_email(note: CreditNote) -> tuple[str, str, str]:
    """Return (subject, plain_text, html) for a credit note."""
    customer = (note.customer_name or 'לקוח/ה').strip()
    before_vat, vat_amount, gross = split_vat_inclusive(note.amount)
    issued = _fmt_date(note.credited_on)
    original_date = _fmt_date(note.original_date)

    ref = note.document_number or note.original_number
    subject = f'{DOCUMENT_TITLE} {ref} — קוגומלו'.replace('  ', ' ').strip()

    # סעיף 9(ה)(4) — the invoice this credit refers to, by number and date.
    original_line = ''
    if note.original_number:
        original_line = f'זיכוי עבור חשבונית {note.original_number}'
        if original_date:
            original_line += f' מתאריך {original_date}'

    detail_rows = []
    if note.document_number:
        detail_rows.append(('מספר הודעת זיכוי', note.document_number))
    detail_rows.append(('תאריך', issued))
    if original_line:
        detail_rows.append(('מתייחס ל', original_line.replace('זיכוי עבור ', '')))
    detail_rows.append(('סיבת הזיכוי', note.reason or 'זיכוי'))

    text = (
        f'שלום {customer},\n\n'
        f'זוהי {DOCUMENT_TITLE} עבור תשלום שבוצע בקוגומלו.\n\n'
        + '\n'.join(f'{label}: {value}' for label, value in detail_rows)
        + f'\n\nסכום הזיכוי לפני מע"מ: ₪{before_vat:.2f}\n'
        + f'מס ערך מוסף {VAT_PERCENT_DISPLAY:g}%: ₪{vat_amount:.2f}\n'
        + f'סה"כ זיכוי כולל מע"מ: ₪{gross:.2f}\n\n'
        'הזיכוי הועבר לחברת האשראי. מועד ההחזר בפועל תלוי בחברת האשראי,\n'
        'ולרוב מופיע בחיוב הבא או בזה שאחריו.\n\n'
        f'{ISSUER_NAME} · עוסק מורשה {ISSUER_COMPANY_NUMBER}\n'
        f'{ISSUER_ADDRESS} · {ISSUER_PHONE}\n\n'
        f'{COMPUTERIZED_MARK}\n\n'
        'בברכה,\nצוות קוגומלו'
    )

    rows_html = ''.join(
        f'<tr>'
        f'<td style="padding:6px 0;color:#666">{label}</td>'
        f'<td style="padding:6px 0;font-weight:bold">{value}</td>'
        f'</tr>'
        for label, value in detail_rows
    )

    html = f'''
<div dir="rtl" style="font-family:Arial,sans-serif;color:#25326a;max-width:620px;margin:auto">
  <h2 style="color:#303094;margin-bottom:4px">{DOCUMENT_TITLE}{f' {note.document_number}' if note.document_number else ''}</h2>
  <p style="color:#888;margin:0 0 18px">{issued}</p>
  <p style="line-height:1.7">שלום <b>{customer}</b>,<br>זוהי {DOCUMENT_TITLE} עבור תשלום שבוצע בקוגומלו.</p>
  <table style="width:100%;border-collapse:collapse;margin-top:14px">{rows_html}</table>
  <table style="width:100%;border-collapse:collapse;margin-top:18px;background:#f7f6fc">
    <tr>
      <td style="padding:8px;color:#666">סכום הזיכוי לפני מע"מ</td>
      <td style="padding:8px;text-align:left" dir="ltr">₪{before_vat:.2f}</td>
    </tr>
    <tr>
      <td style="padding:8px;color:#666">מס ערך מוסף {VAT_PERCENT_DISPLAY:g}%</td>
      <td style="padding:8px;text-align:left" dir="ltr">₪{vat_amount:.2f}</td>
    </tr>
    <tr>
      <td style="padding:8px;font-weight:bold">סה"כ זיכוי כולל מע"מ</td>
      <td style="padding:8px;text-align:left;font-weight:bold" dir="ltr">₪{gross:.2f}</td>
    </tr>
  </table>
  <p style="line-height:1.7;margin-top:18px">
    הזיכוי הועבר לחברת האשראי. מועד ההחזר בפועל תלוי בחברת האשראי,
    ולרוב מופיע בחיוב הבא או בזה שאחריו.
  </p>
  <p style="color:#666;margin-top:22px;line-height:1.8">
    <b>{ISSUER_NAME}</b> · עוסק מורשה {ISSUER_COMPANY_NUMBER}<br>
    {ISSUER_ADDRESS} · <span dir="ltr">{ISSUER_PHONE}</span>
  </p>
  <p style="margin-top:16px;padding:8px 12px;background:#eef1f5;color:#303094;font-weight:bold;display:inline-block">
    {COMPUTERIZED_MARK}
  </p>
  <p style="color:#666;margin-top:18px">בברכה,<br>צוות קוגומלו</p>
</div>'''

    return subject, text, html


def send_credit_note_email(
    note: CreditNote,
    *,
    pdf_bytes: bytes | None = None,
    pdf_filename: str = '',
) -> bool:
    """Email the customer their credit note. Returns True if it was sent."""
    email = (note.email or '').strip()
    label = note.document_number or note.original_number or '—'

    if not email:
        logger.info('Skipping credit note email for %s: no customer email', label)
        return False

    if not _email_configured():
        logger.warning('No email provider configured — cannot send credit note %s to %s', label, email)
        return False

    subject, text, html = build_credit_note_email(note)
    attachments = None
    if pdf_bytes:
        filename = pdf_filename or f'{label}.pdf'
        attachments = [{'filename': filename, 'content': base64.b64encode(pdf_bytes).decode('ascii')}]

    if resend_configured():
        send_resend_email(to=[email], subject=subject, text=text, html=html, attachments=attachments)
    else:
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@kogomalo.com')
        message = EmailMultiAlternatives(subject, text, from_email, [email])
        message.attach_alternative(html, 'text/html')
        if pdf_bytes:
            message.attach(pdf_filename or f'{label}.pdf', pdf_bytes, 'application/pdf')
        message.send(fail_silently=False)

    logger.info('Sent credit note email %s → %s', label, email)
    return True
