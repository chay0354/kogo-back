"""
The app's own switches (docs/WAHUB-CONTRACT-REFINE.md, א) — one small key/value
table, `WahubSetting`.

Why a table of its own: until now the app kept two kinds of "settings", and
neither fits a switch. The office hours are a KnowledgeItem — everything there
is read out to the bot. The inbound key is an IntegrationCredential — a secret:
write-only over the API, and overridden by the environment. A switch the owner
flips from the screen, and a marker the cron moves, are neither.

    review_auto_apply       the owner's standing approval of fixes to the bot's
                            knowledge. True until somebody turns it off — his
                            decision of 11.10.2026 ("אני מאשר אותם כבר מעכשיו").
    review_history_cursor   where the one-time sweep over the old conversations
                            has reached (reviewer.scan_history).
    lead_followup_templates the approved WhatsApp template (a ManyChat flow) behind
                            each follow-up to a lead that leaves after the 24-hour
                            window: {"link_reminder_1": "<flow ns>", …}. Empty until
                            the owner has made the templates — and a follow-up with
                            no template stays "due" (lead_followups.py).
    lead_dormant_on         the day dormant leads are written to once (YYYY-MM-DD:
                            registration opens, a season starts). Empty: never.
"""
from __future__ import annotations

from apps.wahub.models import WahubSetting

REVIEW_AUTO_APPLY = 'review_auto_apply'
REVIEW_HISTORY_CURSOR = 'review_history_cursor'
LEAD_FOLLOWUP_TEMPLATES = 'lead_followup_templates'
LEAD_DORMANT_ON = 'lead_dormant_on'


def row(key: str) -> WahubSetting | None:
    return WahubSetting.objects.filter(key=key).select_related('updated_by').first()


def get(key: str, default=None):
    """The stored value; `default` when nobody has set one."""
    found = WahubSetting.objects.filter(key=key).values_list('value', flat=True)[:1]
    for value in found:
        return default if value is None else value
    return default


def put(key: str, value, user=None) -> WahubSetting:
    saved, _ = WahubSetting.objects.update_or_create(
        key=key, defaults={'value': value, 'updated_by': user if getattr(user, 'is_authenticated', False) else None},
    )
    return saved


def review_auto_apply() -> bool:
    return bool(get(REVIEW_AUTO_APPLY, True))


def set_review_auto_apply(value: bool, user=None) -> WahubSetting:
    return put(REVIEW_AUTO_APPLY, bool(value), user)


def lead_templates() -> dict:
    """{template key: ManyChat flow} — only the keys somebody mapped to a flow."""
    kept = get(LEAD_FOLLOWUP_TEMPLATES, {})
    if not isinstance(kept, dict):
        return {}
    return {str(key): str(flow).strip() for key, flow in kept.items() if isinstance(flow, str) and flow.strip()}
