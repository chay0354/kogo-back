"""
The office hears about a customer's WhatsApp that did not go out.

Until 30.9.2026 a failed send — a broadcast row, a trial reminder, a "payment
failed" notice — was a line in the server log and nothing else: nobody was
told, and the parent simply never heard. Every failure now becomes an office
alert (apps/core/office_alerts.py) with who, what, why and what to do.

One alert per phone per day: the trial-reminder cron retries every half hour,
and a broadcast may send twice to the same number.

In the system only: the owner asked (30.9.2026) that these never reach the
office's WhatsApp — they wait in the morning brief ("התראות למשרד"). The
alerts about payments and registrations still go out as before.

A free-text fallback is reported too, once a day per message type: ManyChat
accepts it and Kogo marks it sent, but WhatsApp delivers free text only to
someone who wrote to the business in the last 24 hours — to everyone else it
silently never arrives.

Nothing here raises: an alert must never break the send it reports on.
"""
from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

KIND_FAILED = 'whatsapp_failed'
KIND_FREE_TEXT = 'whatsapp_free_text'

WHERE_SETTINGS = 'הגדרות › הודעות › "אנשי קשר ש-ManyChat לא מוצא"'

# reason → (why, what to do)
REASONS = {
    'contact_unfindable': (
        'איש הקשר קיים ב-ManyChat, אבל חסר לו השדה שלפיו קוגו מוצאת אותו.',
        f'{WHERE_SETTINGS}: להוריד את הקובץ ולייבא אותו ב-ManyChat. '
        'או לקשר את איש הקשר ידנית מתוצאות התפוצה.',
    ),
    'not_on_whatsapp': (
        'המספר לא רשום בוואטסאפ.',
        'לבדוק את מספר הטלפון בכרטיס הלקוח ולתקן אותו.',
    ),
    'lookup_failed': (
        'ManyChat לא ענה כשחיפשנו את איש הקשר.',
        'לשלוח שוב מאוחר יותר. אם זה חוזר — לדווח למפתח.',
    ),
    'no_subscriber_id': (
        'ManyChat לא החזיר איש קשר.',
        'לשלוח שוב. אם זה חוזר — לדווח למפתח.',
    ),
    'send_flow_failed': (
        'ManyChat סירב להפעיל את האוטומציה.',
        'לבדוק ב-ManyChat שהאוטומציה קיימת ופעילה, ואז לשלוח שוב.',
    ),
    'send_text_failed': (
        'ManyChat סירב לשלוח את ההודעה.',
        'לבדוק את איש הקשר ב-ManyChat, ואז לשלוח שוב.',
    ),
    'link_host_not_configured': (
        'בהודעה היה קישור שנפתח רק במחשב של מפתח, ולכן היא לא נשלחה.',
        'לדווח למפתח: CRM_FRONTEND_URL לא מוגדר בשרת.',
    ),
}
DEFAULT_REASON = ('ManyChat החזיר שגיאה.', 'לשלוח שוב. אם זה חוזר — לדווח למפתח.')


def _israel_day_start():
    now = timezone.localtime(timezone.now())
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def alert_send_failure(
    *,
    phone: str,
    where: str,
    parent_name: str = '',
    child_name: str = '',
    reason: str = '',
    error: str = '',
    link: str = '',
) -> None:
    """A WhatsApp to a customer did not go out. Never raises."""
    try:
        from apps.core.manychat_service import ManyChatService
        from apps.core.office_alerts import raise_office_alert

        # No key at all is one fact about the server, not one per parent: the
        # brief's WhatsApp health check reports it every morning. (It is also
        # every local and test run, which must not fill the office's list.)
        if not ManyChatService().is_configured:
            return
        key = ManyChatService.normalize_phone_e164(phone) or str(phone or '')
        day = _israel_day_start()
        why, action = REASONS.get(reason, DEFAULT_REASON)
        raise_office_alert(
            kind=KIND_FAILED,
            dedup_key=f'{KIND_FAILED}:{key}:{day.date().isoformat()}',
            title=f'וואטסאפ לא יצא ל-{parent_name or key}',
            where=where,
            what=error or why,
            why=why,
            customer=' · '.join(part for part in (parent_name, key, f'ילד: {child_name}' if child_name else '') if part),
            action=action,
            link=link,
            details={'phone': key, 'reason': reason, 'error': error},
            deliver=False,
        )
    except Exception:  # noqa: BLE001 — an alert must never break the send it reports on
        logger.exception('WhatsApp failure alert for %s could not be raised', phone)


def alert_free_text(*, label: str, flow_setting: str) -> None:
    """A message went out as free text because no automation is set for it. Once a day per type."""
    try:
        from apps.core.office_alerts import raise_office_alert

        day = _israel_day_start()
        raise_office_alert(
            kind=KIND_FREE_TEXT,
            dedup_key=f'{KIND_FREE_TEXT}:{flow_setting}:{day.date().isoformat()}',
            title=f'"{label}" יצאה כטקסט חופשי — ייתכן שלא הגיעה',
            where='שליחת וואטסאפ ללקוח',
            what=f'אין ב-ManyChat אוטומציה להודעת "{label}", ולכן נשלח טקסט חופשי.',
            why='וואטסאפ מוסר טקסט חופשי רק למי שכתב לעסק ב-24 השעות האחרונות. '
                'לכל השאר ההודעה לא מגיעה, ו-ManyChat לא מדווח על כך.',
            action=f'ליצור ב-ManyChat אוטומציה עם תבנית להודעה הזו, ולהגדיר אותה ב-{flow_setting}.',
            details={'flow_setting': flow_setting},
            deliver=False,
        )
    except Exception:  # noqa: BLE001
        logger.exception('Free-text alert for %s could not be raised', flow_setting)
