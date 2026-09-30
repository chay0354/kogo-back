"""
Send store invoice / receipt emails to website customers after successful payment.

Since 25.9.2026 the same exit also mails a till sale's signed original, when the
signing service asks for it (`any_sale`): every original reaches its customer
(the owner's decision D5), and a till sale used to reach nobody.
"""
from __future__ import annotations

import base64
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from apps.core.resend_email import resend_configured, send_resend_email
from apps.core.vat import DOCUMENT_TITLE, split_vat_inclusive
from apps.store.invoice_pdf import generate_store_invoice_pdf
from apps.store.models import StoreInvoice

logger = logging.getLogger(__name__)


def _email_configured() -> bool:
    if resend_configured():
        return True
    host = getattr(settings, 'EMAIL_HOST', '') or ''
    return bool(host.strip())


def build_store_invoice_email(invoice: StoreInvoice) -> tuple[str, str, str]:
    """Return (subject, plain_text, html) for a completed store invoice."""
    customer = (invoice.customer_name or 'לקוח/ה').strip()
    if customer == 'לקוח/ה' and invoice.child_id:
        customer = invoice.child.full_name or customer
    order_ref = invoice.website_order_number or invoice.invoice_number
    doc_ref = invoice.invoice_number
    # A till sale has no website order to name, and a sale put on the monthly
    # standing order is a חשבונית עסקה (StoreSaleSource.type_label), not a receipt.
    title = 'חשבונית עסקה' if invoice.payment_method == 'monthly_billing' else DOCUMENT_TITLE
    subject = (f'{title} {doc_ref} — הזמנה {order_ref}' if invoice.website_order_number
               else f'{title} {doc_ref} — קוגומלו')

    lines = []
    html_rows = []
    for sale in invoice.line_items.select_related('product').all():
        name = sale.product.name if sale.product_id else 'פריט'
        if sale.size:
            name = f'{name} ({sale.size})'
        line_total = sale.total_price
        lines.append(f'• {name} × {sale.quantity} — ₪{line_total:.2f}')
        html_rows.append(
            f'<tr>'
            f'<td style="padding:8px;border-bottom:1px solid #eee">{name}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;text-align:center">{sale.quantity}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;text-align:left" dir="ltr">₪{line_total:.2f}</td>'
            f'</tr>'
        )

    if not lines:
        lines.append('• (פרטי הפריטים יופיעו בחשבונית במערכת)')
        html_rows.append(
            '<tr><td colspan="3" style="padding:8px;color:#666">פרטי הפריטים יופיעו בחשבונית במערכת</td></tr>'
        )

    issue = timezone.localtime(invoice.issue_date).strftime('%d/%m/%Y %H:%M')
    txn = (invoice.tranzila_confirmation_code or invoice.tranzila_transaction_id or '').strip()
    txn_line = f'\nאישור תשלום: {txn}' if txn else ''
    before_vat, vat_amount, gross = split_vat_inclusive(invoice.total_amount)

    order_line = f'מספר הזמנה: {order_ref}\n' if invoice.website_order_number else ''
    text = (
        f'שלום {customer},\n\n'
        f'תודה על הרכישה בחנות קוגומלו!\n\n'
        f'מספר {title}: {doc_ref}\n'
        f'{order_line}'
        f'תאריך: {issue}\n'
        f'{txn_line}\n'
        f'המסמך מצורף למייל בקובץ PDF.\n\n'
        f'פריטים:\n'
        + '\n'.join(lines)
        + f'\n\nסה"כ לפני מע"מ: ₪{before_vat:.2f}\n'
        + f'מע"מ 18%: ₪{vat_amount:.2f}\n'
        + f'סה"כ כולל מע"מ: ₪{gross:.2f}\n\n'
        f'בברכה,\nצוות קוגומלו'
    )

    html = f'''
<div dir="rtl" style="font-family:Arial,sans-serif;color:#25326a;max-width:620px;margin:auto">
  <h2 style="color:#303094">{title} {doc_ref}</h2>
  <p style="color:#888;margin:0 0 16px">{f"הזמנה {order_ref} · " if invoice.website_order_number else ""}{issue}</p>
  <p style="line-height:1.7">שלום <b>{customer}</b>,<br>תודה על הרכישה בחנות קוגומלו!</p>
  {"<p><b>אישור תשלום:</b> " + txn + "</p>" if txn else ""}
  <p style="margin-top:12px;color:#303094;font-weight:bold">המסמך מצורף למייל בקובץ PDF.</p>
  <table style="width:100%;border-collapse:collapse;margin-top:12px">
    <thead>
      <tr style="background:#f7f6fc">
        <th style="padding:8px;text-align:right">מוצר</th>
        <th style="padding:8px;text-align:center">כמות</th>
        <th style="padding:8px;text-align:left">סה"כ</th>
      </tr>
    </thead>
    <tbody>{"".join(html_rows)}</tbody>
  </table>
  <p style="margin-top:16px;line-height:1.8">
    סה"כ לפני מע"מ: <span dir="ltr">₪{before_vat:.2f}</span><br>
    מע"מ 18%: <span dir="ltr">₪{vat_amount:.2f}</span><br>
    <b>סה"כ כולל מע"מ: <span dir="ltr">₪{gross:.2f}</span></b>
  </p>
  <p style="color:#666;margin-top:24px">בברכה,<br>צוות קוגומלו</p>
</div>'''

    return subject, text, html


def send_store_invoice_email(invoice: StoreInvoice, *, email: str = '', any_sale: bool = False) -> bool:
    """
    Email the customer their invoice after a successful website store purchase.
    Idempotent — skips if already sent or email is missing.

    `any_sale` is the signing service's call (signing.service._send_by_channel):
    a till sale is mailed too, and so is a sale refunded after it was issued —
    its original is still owed — to `email` when the office typed one, else the
    buyer's address, else the child's family's. Without it, as before: a paid
    website order, to the address it was placed with.
    """
    if invoice.invoice_email_sent_at:
        return True

    if any_sale:
        from apps.documents.signing.sources import StoreSaleSource

        if not StoreSaleSource(invoice).issued():
            return False
    else:
        if invoice.payment_status != 'completed':
            return False
        if not invoice.website_order_number:
            return False

    email = (email or invoice.customer_email or '').strip()
    if not email and any_sale:
        from apps.documents.signing.sources import StoreSaleSource

        email = StoreSaleSource(invoice).default_email
    if not email:
        logger.info('Skipping invoice email for %s: no customer_email', invoice.invoice_number)
        return False

    if not _email_configured():
        logger.warning(
            'No email provider configured — cannot send invoice %s to %s',
            invoice.invoice_number,
            email,
        )
        return False

    invoice = (
        StoreInvoice.objects
        .select_related('child')
        .prefetch_related('line_items__product')
        .get(pk=invoice.pk)
    )

    from apps.documents import signing

    claim = None
    if signing.enabled():
        # The signed original stored when the sale was paid, or no mail at all
        # (the row says why — paper, held, or already sent).
        from apps.documents.models import SignedOriginal
        from apps.documents.signing.service import KIND_STORE, claim_email

        claim = claim_email(KIND_STORE, invoice, channel=SignedOriginal.CHANNEL_STORE, email_to=email)
        if claim is None:
            return False

    subject, text, html = build_store_invoice_email(invoice)
    pdf_bytes = claim.pdf if claim is not None else generate_store_invoice_pdf(invoice)
    filename = f'{invoice.invoice_number}.pdf'

    try:
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
    except Exception as exc:
        if claim is not None:
            claim.failed(exc)
        raise
    if claim is not None:
        claim.sent()

    StoreInvoice.objects.filter(pk=invoice.pk).update(invoice_email_sent_at=timezone.now())
    logger.info('Sent store invoice email %s → %s', invoice.invoice_number, email)
    return True
