"""Send CRM password-reset emails via Resend (same provider as invoices)."""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.core.resend_email import resend_configured, send_resend_email

User = get_user_model()
logger = logging.getLogger(__name__)


def _email_configured() -> bool:
    if resend_configured():
        return True
    return bool((getattr(settings, 'EMAIL_HOST', '') or '').strip())


def crm_frontend_url() -> str:
    explicit = (getattr(settings, 'CRM_FRONTEND_URL', '') or '').strip()
    if explicit:
        return explicit.rstrip('/')
    for origin in getattr(settings, 'CORS_ALLOWED_ORIGINS', []) or []:
        origin = (origin or '').strip()
        if origin.startswith('http'):
            return origin.rstrip('/')
    return 'http://localhost:3000'


def build_password_reset_link(user: User) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return f'{crm_frontend_url()}/reset-password?uid={uid}&token={token}'


def build_password_reset_email(user: User) -> tuple[str, str, str, str]:
    link = build_password_reset_link(user)
    name = (user.first_name or user.email or 'משתמש/ת').strip()
    subject = 'איפוס סיסמה — מערכת קוגומלו'
    text = (
        f'שלום {name},\n\n'
        f'קיבלנו בקשה לאיפוס הסיסמה שלך במערכת הפנימית של קוגומלו.\n\n'
        f'לאיפוס הסיסמה, לחץ/י על הקישור הבא (תוקף 24 שעות):\n{link}\n\n'
        f'אם לא ביקשת איפוס סיסמה, ניתן להתעלם מהודעה זו.\n\n'
        f'בברכה,\nצוות קוגומלו'
    )
    html = f'''
<div dir="rtl" style="font-family:Arial,sans-serif;color:#25326a;max-width:620px;margin:auto">
  <h2 style="color:#303094">איפוס סיסמה</h2>
  <p style="line-height:1.7">שלום {name},<br>קיבלנו בקשה לאיפוס הסיסמה שלך במערכת הפנימית של קוגומלו.</p>
  <p style="margin:24px 0">
    <a href="{link}" style="display:inline-block;background:#303094;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:bold">
      איפוס סיסמה
    </a>
  </p>
  <p style="font-size:13px;color:#666;line-height:1.6">
    אם הכפתור לא עובד, העתק/י את הקישור לדפדפן:<br>
    <span dir="ltr" style="word-break:break-all">{link}</span>
  </p>
  <p style="color:#666;margin-top:24px">אם לא ביקשת איפוס סיסמה, ניתן להתעלם מהודעה זו.<br>בברכה,<br>צוות קוגומלו</p>
</div>'''
    return subject, text, html, link


def send_password_reset_email(user: User) -> bool:
    if not _email_configured():
        logger.warning('No email provider — cannot send password reset to %s', user.email)
        return False

    subject, text, html, _ = build_password_reset_email(user)
    email = (user.email or '').strip()
    if not email:
        return False

    if resend_configured():
        send_resend_email(to=[email], subject=subject, text=text, html=html)
    else:
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@kogomalo.com')
        message = EmailMultiAlternatives(subject, text, from_email, [email])
        message.attach_alternative(html, 'text/html')
        message.send(fail_silently=False)

    logger.info('Sent password reset email to %s', email)
    return True
