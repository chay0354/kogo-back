"""Email subscription invoices (registration + monthly) with letterhead PDF."""
from __future__ import annotations

import base64
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from apps.core.resend_email import resend_configured, send_resend_email
from apps.core.vat import DOCUMENT_TITLE, split_vat_inclusive
from apps.customers.financial_models import Invoice

logger = logging.getLogger(__name__)


def _email_configured() -> bool:
    if resend_configured():
        return True
    return bool((getattr(settings, 'EMAIL_HOST', '') or '').strip())


def build_subscription_invoice_email(invoice: Invoice) -> tuple[str, str, str]:
    invoice = Invoice.objects.select_related('family').prefetch_related('children__child', 'children__course').get(
        pk=invoice.pk,
    )
    child_names = ', '.join(
        e.child.full_name for e in invoice.children.all() if e.child_id
    ) or 'הרשמה לחוג'
    subject = f'{DOCUMENT_TITLE} {invoice.invoice_number} — קוגומלו'
    issue = timezone.localtime(invoice.invoice_date).strftime('%d/%m/%Y')
    before_vat, vat_amount, gross = split_vat_inclusive(invoice.amount)

    text = (
        f'שלום,\n\n'
        f'תודה על התשלום!\n\n'
        f'מספר {DOCUMENT_TITLE}: {invoice.invoice_number}\n'
        f'תאריך: {issue}\n'
        f'ילד/ים: {child_names}\n'
        f'סה"כ לפני מע"מ: ₪{before_vat:.2f}\n'
        f'מע"מ 18%: ₪{vat_amount:.2f}\n'
        f'סה"כ כולל מע"מ: ₪{gross:.2f}\n\n'
        f'המסמך מצורף למייל בקובץ PDF.\n\n'
        f'בברכה,\nצוות קוגומלו'
    )
    html = f'''
<div dir="rtl" style="font-family:Arial,sans-serif;color:#25326a;max-width:620px;margin:auto">
  <h2 style="color:#303094">{DOCUMENT_TITLE} {invoice.invoice_number}</h2>
  <p style="line-height:1.7">שלום,<br>תודה על התשלום!</p>
  <p>ילד/ים: <b>{child_names}</b></p>
  <p style="line-height:1.8">
    סה"כ לפני מע"מ: <span dir="ltr">₪{before_vat:.2f}</span><br>
    מע"מ 18%: <span dir="ltr">₪{vat_amount:.2f}</span><br>
    <b>סה"כ כולל מע"מ: <span dir="ltr">₪{gross:.2f}</span></b>
  </p>
  <p style="color:#303094;font-weight:bold;margin-top:12px">המסמך מצורף למייל בקובץ PDF.</p>
  <p style="color:#666;margin-top:24px">בברכה,<br>צוות קוגומלו</p>
</div>'''
    return subject, text, html


def send_subscription_invoice_email(invoice: Invoice) -> bool:
    """Send invoice PDF to payer email. Idempotent via invoice.email_sent_at."""
    if invoice.email_sent_at:
        return True

    email = (invoice.payer_email or '').strip()
    if not email and invoice.family_id:
        email = (invoice.family.email or '').strip()
    if not email:
        logger.info('Skipping subscription invoice email for %s: no payer email', invoice.invoice_number)
        return False

    if not _email_configured():
        logger.warning('No email provider — cannot send invoice %s', invoice.invoice_number)
        return False

    subject, text, html = build_subscription_invoice_email(invoice)
    from apps.customers.subscription_invoice_pdf import generate_subscription_invoice_pdf
    pdf_bytes = generate_subscription_invoice_pdf(invoice)
    filename = f'{invoice.invoice_number}.pdf'

    if resend_configured():
        send_resend_email(
            to=[email],
            subject=subject,
            text=text,
            html=html,
            attachments=[{
                'filename': filename,
                'content': base64.b64encode(pdf_bytes).decode('ascii'),
            }],
        )
    else:
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@kogomalo.com')
        message = EmailMultiAlternatives(subject, text, from_email, [email])
        message.attach_alternative(html, 'text/html')
        message.attach(filename, pdf_bytes, 'application/pdf')
        message.send(fail_silently=False)

    Invoice.objects.filter(pk=invoice.pk).update(email_sent_at=timezone.now())
    logger.info('Sent subscription invoice email %s → %s', invoice.invoice_number, email)
    return True
