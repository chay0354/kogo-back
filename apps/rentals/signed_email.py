"""Email the tenant their signed rental contract, once the signing has committed.

Sent the way the invoices are (apps/store/invoice_email.py): Resend when it is
configured, else Django's mail backend when a host is. The copy is the one on
file, checked against its fingerprint first. It goes to the email the contract
states (its frozen terms), the address the tenant saw on the page they signed.

The signing is the record; this email is a courtesy. It runs from
transaction.on_commit, and send_signed_contract_email never raises: a failure
is logged, and the office can still download the copy and send it on.
"""
from __future__ import annotations

import base64
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone
from django.utils.html import escape

from apps.core.resend_email import resend_configured, send_resend_email
from apps.rentals.models import RentalContract

logger = logging.getLogger(__name__)


def _email_configured() -> bool:
    if resend_configured():
        return True
    return bool((getattr(settings, 'EMAIL_HOST', '') or '').strip())


def signed_copy_filename(contract) -> str:
    return f'rental-contract-v{contract.version}-signed.pdf'


def build_signed_contract_email(contract) -> tuple[str, str, str]:
    """(subject, plain text, html) for a signed contract."""
    terms = contract.terms or {}
    name = ((terms.get('tenant') or {}).get('name') or '').strip() or 'שוכר/ת'
    branch = ((terms.get('branch') or {}).get('name') or '').strip()
    signed_at = timezone.localtime(contract.signed_at).strftime('%d/%m/%Y %H:%M')
    where = f' בסניף {branch}' if branch else ''
    subject = f'חוזה השכירות החתום — גרסה {contract.version}'
    text = (
        f'שלום {name},\n\n'
        f'תודה שחתמת על חוזה השכירות{where} (גרסה {contract.version}).\n'
        f'נחתם ב־{signed_at}.\n\n'
        'העותק החתום מצורף למייל בקובץ PDF.\n\n'
        'בברכה,\nצוות קוגומלו'
    )
    html = f'''
<div dir="rtl" style="font-family:Arial,sans-serif;color:#25326a;max-width:620px;margin:auto">
  <h2 style="color:#303094">חוזה השכירות החתום</h2>
  <p style="line-height:1.7">שלום <b>{escape(name)}</b>,<br>
  תודה שחתמת על חוזה השכירות{escape(where)} (גרסה {contract.version}).<br>
  נחתם ב־<span dir="ltr">{signed_at}</span>.</p>
  <p style="color:#303094;font-weight:bold;margin-top:12px">העותק החתום מצורף למייל בקובץ PDF.</p>
  <p style="color:#666;margin-top:24px">בברכה,<br>צוות קוגומלו</p>
</div>'''
    return subject, text, html


def send_signed_contract_email(contract_id) -> bool:
    """Email the signed copy to the tenant. True when sent; never raises."""
    try:
        return _send_signed_contract_email(contract_id)
    except Exception:
        logger.exception(
            'Signed rental contract %s: emailing the signed copy failed; the signing stands', contract_id,
        )
        return False


def _send_signed_contract_email(contract_id) -> bool:
    contract = RentalContract.objects.get(pk=contract_id)
    if contract.status != RentalContract.STATUS_SIGNED:
        logger.warning('Rental contract %s is not signed; no signed copy to email', contract_id)
        return False
    email = (((contract.terms or {}).get('tenant') or {}).get('email') or '').strip()
    if not email:
        logger.info('Signed rental contract %s: the contract states no email; the copy was not emailed', contract_id)
        return False
    if not _email_configured():
        logger.warning('No email provider configured — cannot email signed rental contract %s', contract_id)
        return False
    if not contract.signed_pdf_is_intact():
        logger.error(
            'Signed rental contract %s: the stored signed copy does not match its SHA-256 %s; not emailed',
            contract_id, contract.signed_pdf_sha256,
        )
        return False

    subject, text, html = build_signed_contract_email(contract)
    pdf_bytes = bytes(contract.signed_pdf)
    filename = signed_copy_filename(contract)
    if resend_configured():
        send_resend_email(
            to=[email],
            subject=subject,
            text=text,
            html=html,
            attachments=[{'filename': filename, 'content': base64.b64encode(pdf_bytes).decode('ascii')}],
        )
    else:
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@kogomalo.com')
        message = EmailMultiAlternatives(subject, text, from_email, [email])
        message.attach_alternative(html, 'text/html')
        message.attach(filename, pdf_bytes, 'application/pdf')
        message.send(fail_silently=False)
    logger.info('Emailed signed rental contract %s (version %s) to %s', contract_id, contract.version, email)
    return True
