"""
Mail one of Michal Kagan's documents to her client, with its PDF.

Her clients know her, not the company: the mail is in her name and says what
the document is for, while the document itself carries the issuer's details as
every document kogo issues does. One function for both of her kinds, the
חשבונית מס/קבלה of a payment and the credit note of a refund, since both go out
through her channel (SignedOriginal.CHANNEL_MICHAL) and the signing cron retries
either one the same way.

With signing on, the signed original stored at issue is attached, once
(claim_email); with it off, the document is rendered and sent. A client with
no e-mail, or a deployment with no mail provider, is skipped and logged.
"""
from __future__ import annotations

import base64
import logging
from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from apps.core.resend_email import resend_configured, send_resend_email
from apps.core.vat import DOCUMENT_TITLE, VAT_PERCENT_DISPLAY
from apps.documents.issuer import COMPUTERIZED_MARK, ISSUER_ADDRESS, ISSUER_LINE, ISSUER_PHONE
from apps.documents.models import FormalDocument

logger = logging.getLogger(__name__)

SIGN_OFF = 'מיכל קגן · קונדליני אקטיביישן'
CREDIT_TITLE = 'חשבונית מס זיכוי'


def _email_configured() -> bool:
    if resend_configured():
        return True
    return bool((getattr(settings, 'EMAIL_HOST', '') or '').strip())


def build_michal_document_email(doc: FormalDocument) -> tuple[str, str, str]:
    """(subject, plain text, html)."""
    name = (doc.business_customer.full_name if doc.business_customer_id else doc.customer_name) or 'שלום'
    credit = doc.document_type == 'credit_invoice'
    title = CREDIT_TITLE if credit else DOCUMENT_TITLE
    net = doc.subtotal - doc.discount_amount
    issued = doc.document_date.strftime('%d/%m/%Y')
    subject = f'{title} {doc.document_number} — {SIGN_OFF}'

    if credit:
        what = (
            f'זיכוי עבור {DOCUMENT_TITLE} {doc.linked_document_number}'
            + (f' מתאריך {doc.linked_document_date:%d/%m/%Y}' if doc.linked_document_date else '')
        )
        opening = 'מצורפת הודעת הזיכוי עבור ההחזר שבוצע.'
        reason = f'סיבה: {doc.credit_reason}\n' if doc.credit_reason else ''
    else:
        line = doc.line_items.first()
        what = (line.description if line else doc.description) or 'טיפול קונדליני אקטיביישן'
        opening = 'תודה על התשלום!'
        reason = ''
    payment = None if credit else doc.payments.first()
    paid = ''
    if payment is not None:
        card = f' ****{payment.card_last_four}' if payment.card_last_four else ''
        paid = f'שולם בכרטיס אשראי{card}' + (f' · {payment.reference}' if payment.reference else '')

    text = (
        f'שלום {name},\n\n{opening}\n\n'
        f'מספר {title}: {doc.document_number}\n'
        f'תאריך: {issued}\n'
        f'עבור: {what}\n'
        f'{reason}'
        f'סה"כ לפני מע"מ: ₪{net:.2f}\n'
        f'מע"מ {VAT_PERCENT_DISPLAY:g}%: ₪{doc.vat_amount:.2f}\n'
        f'סה"כ כולל מע"מ: ₪{doc.total_amount:.2f}\n'
        + (f'{paid}\n' if paid else '')
        + '\nהמסמך מצורף למייל בקובץ PDF.\n\n'
        f'{ISSUER_LINE}\n{ISSUER_ADDRESS} · {ISSUER_PHONE}\n\n'
        f'{COMPUTERIZED_MARK}\n\n'
        f'בברכה,\n{SIGN_OFF}'
    )
    e = escape
    html = f'''
<div dir="rtl" style="font-family:Arial,sans-serif;color:#0d4a4e;max-width:620px;margin:auto">
  <h2 style="color:#1a8a8f">{e(title)} {e(doc.document_number)}</h2>
  <p style="line-height:1.7">שלום <b>{e(name)}</b>,<br>{e(opening)}</p>
  <p>עבור: <b>{e(what)}</b></p>
  {f'<p>{e(reason.strip())}</p>' if reason else ''}
  <p style="line-height:1.8">
    סה"כ לפני מע"מ: <span dir="ltr">₪{net:.2f}</span><br>
    מע"מ {VAT_PERCENT_DISPLAY:g}%: <span dir="ltr">₪{doc.vat_amount:.2f}</span><br>
    <b>סה"כ כולל מע"מ: <span dir="ltr">₪{doc.total_amount:.2f}</span></b>
    {f'<br>{e(paid)}' if paid else ''}
  </p>
  <p style="color:#1a8a8f;font-weight:bold;margin-top:12px">המסמך מצורף למייל בקובץ PDF.</p>
  <p style="color:#666;margin-top:22px;line-height:1.8">{e(ISSUER_LINE)}<br>{e(ISSUER_ADDRESS)} · <span dir="ltr">{e(ISSUER_PHONE)}</span></p>
  <p style="margin-top:16px;padding:8px 12px;background:#e0f7f6;color:#0d4a4e;font-weight:bold;display:inline-block">{e(COMPUTERIZED_MARK)}</p>
  <p style="color:#666;margin-top:18px">בברכה,<br>{e(SIGN_OFF)}</p>
</div>'''
    return subject, text, html


def send_michal_document_email(doc_id) -> bool:
    """Send the document to her client. True when it was sent."""
    from apps.documents.document_pdf import generate_document_pdf
    from apps.documents import signing

    doc = FormalDocument.objects.select_related('business_customer').get(pk=doc_id)
    email = ((doc.business_customer.email if doc.business_customer_id else '') or '').strip()
    if not email:
        logger.info('Michal document %s not e-mailed: the client has no e-mail', doc.document_number)
        return False
    if not _email_configured():
        logger.warning('No e-mail provider — Michal document %s not sent', doc.document_number)
        return False

    claim = None
    if signing.enabled():
        # The signed original stored at issue, or no mail (the row says why).
        from apps.documents.models import SignedOriginal
        from apps.documents.signing.service import KIND_FORMAL, claim_email

        claim = claim_email(KIND_FORMAL, doc, channel=SignedOriginal.CHANNEL_MICHAL, email_to=email)
        if claim is None:
            return False

    subject, text, html = build_michal_document_email(doc)
    pdf_bytes = claim.pdf if claim is not None else generate_document_pdf(doc)
    filename = f'{doc.document_number}.pdf'
    try:
        if resend_configured():
            send_resend_email(
                to=[email], subject=subject, text=text, html=html,
                attachments=[{'filename': filename, 'content': base64.b64encode(pdf_bytes).decode('ascii')}],
            )
        else:
            message = EmailMultiAlternatives(
                subject, text, getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@kogomalo.com'), [email],
            )
            message.attach_alternative(html, 'text/html')
            message.attach(filename, pdf_bytes, 'application/pdf')
            message.send(fail_silently=False)
    except Exception as exc:
        if claim is not None:
            claim.failed(exc)
        raise
    if claim is not None:
        claim.sent()
    logger.info('Michal document %s e-mailed', doc.document_number)
    return True
