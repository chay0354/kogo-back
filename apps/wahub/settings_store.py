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
                            window, with who connected it and when:
                            {"link_reminder_1": {"flow": "<flow ns>", "name": "…",
                            "by": 7, "by_name": "…", "at": "…"}, …}. A bare
                            "<flow ns>" (the first shape of this value) is read the
                            same. Empty until the owner has made the templates — and
                            a follow-up with no template stays "due"
                            (lead_followups.py). Connected on the settings screen
                            (lead_automation.py).
    lead_followup_templates_found
                            what ManyChat last said about the automations that
                            carry the follow-ups' fixed names, and when it was
                            asked: {"at": "…", "ok": true, "flows":
                            {"link_reminder_1": {"flow", "name"}, …}}. A cache
                            kept here because the serverless instances share no
                            other; read and written by lead_templates.py only.
    lead_dormant_on         the day dormant leads are written to once (YYYY-MM-DD:
                            registration opens, a season starts). Empty: never.
    holiday_flows_found     what ManyChat last said about the automations that carry
                            the names of the branches' holidays boards, and when it
                            was asked: {"at": "…", "ok": true, "flows":
                            {"kogo_Holidays": {"flow", "name"}, …}, "twice": […]}.
                            The same kind of cache; read and written by
                            holiday_flows.py only.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.wahub import state
from apps.wahub.models import WahubSetting

REVIEW_AUTO_APPLY = 'review_auto_apply'
REVIEW_HISTORY_CURSOR = 'review_history_cursor'
LEAD_FOLLOWUP_TEMPLATES = 'lead_followup_templates'
LEAD_TEMPLATES_FOUND = 'lead_followup_templates_found'
LEAD_DORMANT_ON = 'lead_dormant_on'
HOLIDAY_FLOWS_FOUND = 'holiday_flows_found'


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


def _template_entry(kept) -> dict:
    """One kept value of the mapping as {flow, name, by, by_name, at}; a bare flow is a flow with nothing else known."""
    if isinstance(kept, str):
        kept = {'flow': kept}
    if not isinstance(kept, dict):
        kept = {}
    flow = kept.get('flow')
    return {
        'flow': flow.strip() if isinstance(flow, str) else '',
        'name': str(kept.get('name') or '').strip(),
        'by': kept.get('by'),
        'by_name': str(kept.get('by_name') or ''),
        'at': kept.get('at') if isinstance(kept.get('at'), str) else None,
    }


def lead_template_entries() -> dict:
    """{template key: {flow, name, by, by_name, at}} for every key somebody has ever set — connected or taken off."""
    kept = get(LEAD_FOLLOWUP_TEMPLATES, {})
    if not isinstance(kept, dict):
        return {}
    return {str(key): _template_entry(value) for key, value in kept.items()}


def lead_templates() -> dict:
    """{template key: ManyChat flow} — only the keys somebody mapped to a flow."""
    return {key: entry['flow'] for key, entry in lead_template_entries().items() if entry['flow']}


def set_lead_template(template_key: str, flow: str, *, name: str = '', user=None) -> dict:
    """
    Connect one follow-up to a ManyChat flow — or take the connection off, with
    an empty `flow`. Who did it and when is kept beside it either way. The whole
    mapping is one row, so the row is held while one key of it is written: two
    people connecting two templates in the same moment lose neither.
    """
    actor = user if getattr(user, 'is_authenticated', False) else None
    entry = {
        'flow': (flow or '').strip(),
        'name': (name or '').strip()[:200] if (flow or '').strip() else '',
        'by': getattr(actor, 'pk', None),
        'by_name': state.user_display_name(actor),
        'at': timezone.now().isoformat(),
    }
    with transaction.atomic():
        try:
            with transaction.atomic():
                row, _made = WahubSetting.objects.select_for_update().get_or_create(
                    key=LEAD_FOLLOWUP_TEMPLATES, defaults={'value': {}},
                )
        except IntegrityError:
            # Somebody made the row in this very moment: it is there now.
            row = WahubSetting.objects.select_for_update().get(key=LEAD_FOLLOWUP_TEMPLATES)
        mapping = dict(row.value) if isinstance(row.value, dict) else {}
        mapping[str(template_key)] = entry
        row.value, row.updated_by = mapping, actor
        row.save(update_fields=['value', 'updated_by', 'updated_at'])
    return entry
