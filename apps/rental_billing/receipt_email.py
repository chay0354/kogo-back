"""E-mail a tenant the חשבונית מס/קבלה of a rental charge, with its PDF.

Sent once per receipt (TenantCharge.receipt_emailed_at). A tenant with no
e-mail, or a deployment with no mail provider, is skipped and logged; a
failure never touches the charge or the receipt (the caller swallows it).
"""
from __future__ import annotations

import base64
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from apps.core.resend_email import resend_configured, send_resend_email
from apps.core.vat import DOCUMENT_TITLE, VAT_PERCENT_DISPLAY
from apps.documents.issuer import COMPUTERIZED_MARK, ISSUER_ADDRESS, ISSUER_LINE, ISSUER_PHONE
from apps.documents.models import FormalDocument
from apps.rental_billing.models import TenantCharge
from apps.rental_billing.schedule import month_label

logger = logging.getLogger(__name__)


def _email_configured() -> bool:
    if resend_configured():
        return True
    return bool((getattr(settings, 'EMAIL_HOST', '') or '').strip())


def build_rental_receipt_email(doc: FormalDocument, charge: TenantCharge) -> tuple[str, str, str]:
    """(subject, plain text, html)."""
    name = (doc.business_customer.full_name if doc.business_customer_id else doc.customer_name) or 'שוכר/ת'
    net = doc.subtotal - doc.discount_amount
    period = month_label(charge.period)
    card = f'כרטיס אשראי ****{charge.card_last4}' if charge.card_last4 else 'כרטיס אשראי'
    confirmation = f' · אישור {charge.confirmation_code}' if charge.confirmation_code else ''
    subject = f'{DOCUMENT_TITLE} {doc.document_number} — קוגומלו'
    issued = doc.document_date.strftime('%d/%m/%Y')

    text = (
        f'שלום {name},\n\n'
        f'תודה על התשלום!\n\n'
        f'מספר {DOCUMENT_TITLE}: {doc.document_number}\n'
        f'תאריך: {issued}\n'
        f'עבור: שכירות סטודיו לחודש {period}\n'
        f'סה"כ לפני מע"מ: ₪{net:.2f}\n'
        f'מע"מ {VAT_PERCENT_DISPLAY:g}%: ₪{doc.vat_amount:.2f}\n'
        f'סה"כ כולל מע"מ: ₪{doc.total_amount:.2f}\n'
        f'שולם ב{card}{confirmation}\n\n'
        f'המסמך מצורף למייל בקובץ PDF.\n\n'
        f'{ISSUER_LINE}\n{ISSUER_ADDRESS} · {ISSUER_PHONE}\n\n'
        f'{COMPUTERIZED_MARK}\n\n'
        'בברכה,\nצוות קוגומלו'
    )
    html = f'''
<div dir="rtl" style="font-family:Arial,sans-serif;color:#25326a;max-width:620px;margin:auto">
  <h2 style="color:#303094">{DOCUMENT_TITLE} {doc.document_number}</h2>
  <p style="line-height:1.7">שלום <b>{name}</b>,<br>תודה על התשלום!</p>
  <p>עבור: <b>שכירות סטודיו לחודש {period}</b></p>
  <p style="line-height:1.8">
    סה"כ לפני מע"מ: <span dir="ltr">₪{net:.2f}</span><br>
    מע"מ {VAT_PERCENT_DISPLAY:g}%: <span dir="ltr">₪{doc.vat_amount:.2f}</span><br>
    <b>סה"כ כולל מע"מ: <span dir="ltr">₪{doc.total_amount:.2f}</span></b><br>
    שולם ב{card}{confirmation}
  </p>
  <p style="color:#303094;font-weight:bold;margin-top:12px">המסמך מצורף למייל בקובץ PDF.</p>
  <p style="color:#666;margin-top:22px;line-height:1.8">{ISSUER_LINE}<br>{ISSUER_ADDRESS} · <span dir="ltr">{ISSUER_PHONE}</span></p>
  <p style="margin-top:16px;padding:8px 12px;background:#eef1f5;color:#303094;font-weight:bold;display:inline-block">{COMPUTERIZED_MARK}</p>
  <p style="color:#666;margin-top:18px">בברכה,<br>צוות קוגומלו</p>
</div>'''
    return subject, text, html


def send_rental_receipt_email(doc_id) -> bool:
    """Send the receipt to the tenant. True when sent now or before."""
    from apps.documents.document_pdf import generate_document_pdf

    doc = FormalDocument.objects.select_related('business_customer').get(pk=doc_id)
    charge = TenantCharge.objects.filter(receipt_id=doc.pk).first()
    if charge is None:
        return False
    if charge.receipt_emailed_at:
        return True
    email = (doc.business_customer.email if doc.business_customer_id else '') or ''
    email = email.strip()
    if not email:
        logger.info('Rental receipt %s not e-mailed: the tenant has no e-mail', doc.document_number)
        return False
    if not _email_configured():
        logger.warning('No e-mail provider — rental receipt %s not sent', doc.document_number)
        return False

    subject, text, html = build_rental_receipt_email(doc, charge)
    pdf_bytes = generate_document_pdf(doc)
    filename = f'{doc.document_number}.pdf'
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
    TenantCharge.objects.filter(pk=charge.pk).update(receipt_emailed_at=timezone.now())
    logger.info('Rental receipt %s e-mailed', doc.document_number)
    return True
