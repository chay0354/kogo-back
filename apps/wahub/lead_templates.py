"""
Which ManyChat automation each follow-up to a lead leaves through, once
WhatsApp's 24 hours are over and only an approved template may leave.

What is connected is always a ManyChat flow (`flow_ns`) — exactly what the
screen's "שלח תבנית" sends (sending.send_flow). A follow-up gets its flow in
one of two ways:

    by a person   connected on the settings screen (lead_automation.connect),
                  kept in settings_store.lead_followup_templates with who and
                  when. What a person decided stands above everything — also
                  when the decision is "none": a follow-up somebody took the
                  connection off stays unconnected.
    by its name   with no decision of a person, the automation ManyChat has
                  under the follow-up's own fixed name (FLOW_NAMES — the names
                  in docs/WAHUB-LEAD-TEMPLATES-FOR-OWNER.md). Capital letters
                  do not matter, nor whether the words are joined by "_", "-"
                  or a space. Two automations under one name are nobody's: the
                  follow-up stays unconnected until a person chooses.

ManyChat is asked for its automations (`getFlows`) only when a follow-up with
no decision of a person needs one, and no more than once in FRESH_FOR: what was
found is kept in the app's own table (settings_store.LEAD_TEMPLATES_FOUND) —
there is no cache the serverless instances share — so the cron does not ask for
every follow-up, and a ManyChat that does not answer leaves the last answer
standing.

Nothing here sends anything.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from django.utils import timezone

from apps.core.manychat_service import ManyChatError, ManyChatService
from apps.wahub import sending, settings_store

logger = logging.getLogger(__name__)

# The five to begin with ("שלב 1" of the owner's document): the first time the bot comes back in
# each of the five common rounds.
STAGE_ONE = ('link_reminder_1', 'will_check_1', 'trial_feedback_1', 'trial_no_show_1', 'date_arrived_age_1')
# template key → the fixed name of its automation in ManyChat. One automation may serve two keys
# (the last reminder is the same words after a link and after a day the customer named).
FLOW_NAMES = {
    'link_reminder_1': 'kogo_lead_link_1',
    'link_reminder_2': 'kogo_lead_link_2',
    'link_reminder_3': 'kogo_lead_last',
    'link_reminder_signup_1': 'kogo_lead_signup_1',
    'link_reminder_commitment_1': 'kogo_lead_commitment_1',
    'will_check_1': 'kogo_lead_check_1',
    'trial_feedback_1': 'kogo_lead_trial_1',
    'trial_feedback_2': 'kogo_lead_trial_2',
    'trial_feedback_3': 'kogo_lead_trial_3',
    'trial_no_show_1': 'kogo_lead_noshow_1',
    'trial_no_show_2': 'kogo_lead_noshow_2',
    'date_arrived_1': 'kogo_lead_date_1',
    'date_arrived_2': 'kogo_lead_date_2',
    'date_arrived_3': 'kogo_lead_last',
    'date_arrived_age_1': 'kogo_lead_age_1',
    'date_arrived_age_2': 'kogo_lead_age_2',
    'date_arrived_not_ready_1': 'kogo_lead_notready_1',
    # Asked what they are looking for, when the two hours end after the office has closed for the weekend.
    'opening_1': 'kogo_lead_opening_1',
}

BY_PERSON = 'manual'
BY_NAME = 'name'
# How long an answer of ManyChat's about its automations is taken as it stands.
FRESH_FOR = timedelta(minutes=10)
# How long ManyChat is waited for: a person may be looking at the settings card meanwhile.
ASK_SECONDS = 8

SIMULATED = 'מחשב פיתוח במצב הדמיה — ManyChat לא נשאל'
NOT_CONNECTED = 'השרת לא מחובר ל-ManyChat'
NO_ANSWER = 'ManyChat לא ענה'


def same_name(name: str) -> str:
    """A name as it is compared: no capitals, and "_", "-" and a space are one thing."""
    return re.sub(r'[\s_\-]+', '_', str(name or '').strip().casefold())


def manychat_flows(*, timeout: int | None = None) -> tuple[list[dict] | None, str]:
    """
    (the automations ManyChat has, as [{id, name}] — `id` is the flow a send
    goes through — and ''); or (None, why they cannot be read): no key on the
    server, ManyChat did not answer, a developer's machine that only records.
    """
    if sending.simulate_send():
        return None, SIMULATED
    service = ManyChatService()
    if not service.is_configured:
        return None, NOT_CONNECTED
    try:
        flows = service.get_flows(timeout=timeout)
    except ManyChatError as exc:
        logger.warning('wahub lead templates: ManyChat did not list its automations: %s', exc)
        return None, NO_ANSWER
    found, seen = [], set()
    for flow in flows:
        flow_ns = str(flow.get('ns') or flow.get('flow_ns') or '').strip()
        if not flow_ns or flow_ns in seen:
            continue
        seen.add(flow_ns)
        found.append({'id': flow_ns, 'name': str(flow.get('name') or flow.get('title') or '').strip() or flow_ns})
    found.sort(key=lambda row: (row['name'].casefold(), row['id']))
    return found, ''


def by_name(flows: list[dict]) -> dict:
    """{template key: {flow, name}} for every follow-up whose fixed name exactly one automation carries."""
    named: dict = {}
    for flow in flows:
        named.setdefault(same_name(flow['name']), []).append(flow)
    found = {}
    for template_key, name in FLOW_NAMES.items():
        carrying = named.get(same_name(name)) or []
        if len(carrying) == 1:
            found[template_key] = {'flow': carrying[0]['id'], 'name': carrying[0]['name']}
        elif carrying:
            logger.warning('wahub lead templates: %d automations are named %s; %s is left unconnected', len(carrying), name, template_key)
    return found


def remember(flows: list[dict], now=None) -> dict:
    """Keep what ManyChat's list says about the fixed names, with the moment it said so."""
    named = by_name(flows)
    settings_store.put(settings_store.LEAD_TEMPLATES_FOUND, {'at': (now or timezone.now()).isoformat(), 'ok': True, 'flows': named})
    return named


def _kept() -> tuple[dict, datetime | None]:
    kept = settings_store.get(settings_store.LEAD_TEMPLATES_FOUND, {})
    kept = kept if isinstance(kept, dict) else {}
    flows = kept.get('flows') if isinstance(kept.get('flows'), dict) else {}
    try:
        at = datetime.fromisoformat(kept.get('at') or '')
        at = at if timezone.is_aware(at) else timezone.make_aware(at)
    except (TypeError, ValueError):
        at = None
    clean = {
        str(key): {'flow': str(row.get('flow') or '').strip(), 'name': str(row.get('name') or '').strip()}
        for key, row in flows.items() if isinstance(row, dict) and str(row.get('flow') or '').strip()
    }
    return clean, at


def found(now=None, *, ask: bool = True) -> dict:
    """
    {template key: {flow, name}} — the automations found by their fixed names.
    The kept answer while it is fresh; otherwise ManyChat is asked once (when
    `ask` allows and there is a ManyChat to ask) and the answer kept. When it
    does not answer, the last answer stands and the next asking waits its turn.
    """
    now = now or timezone.now()
    kept, at = _kept()
    if not ask or (at is not None and abs(now - at) < FRESH_FOR):
        return kept
    if sending.simulate_send() or not ManyChatService().is_configured:
        return kept
    flows, _why = manychat_flows(timeout=ASK_SECONDS)
    if flows is None:
        settings_store.put(settings_store.LEAD_TEMPLATES_FOUND, {'at': now.isoformat(), 'ok': False, 'flows': kept})
        return kept
    return remember(flows, now)


def _decided() -> dict:
    """
    {template key: entry} for what a person decided on the screen: a flow, or —
    an entry with no flow and with its moment — "none". A blank left by the
    first shape of the mapping is no decision.
    """
    return {
        key: entry for key, entry in settings_store.lead_template_entries().items()
        if entry['flow'] or entry['at']
    }


def connections(now=None, *, ask: bool = True) -> dict:
    """
    {template key: {flow, name, by, by_name, at}} — every follow-up that has an
    automation to leave through, and how it got it (`by`: a person on the
    screen, or its fixed name). ManyChat is asked only when some follow-up is
    still open to being found by name.
    """
    decided = _decided()
    connected = {
        key: {**entry, 'name': entry['name'] or entry['flow'], 'by': BY_PERSON}
        for key, entry in decided.items() if entry['flow']
    }
    if all(key in decided for key in FLOW_NAMES):
        return connected
    for key, row in found(now, ask=ask).items():
        if key not in decided:
            connected[key] = {'flow': row['flow'], 'name': row['name'] or row['flow'], 'by': BY_NAME, 'by_name': '', 'at': None}
    return connected


def flow_for(template_key: str, now=None) -> str:
    """The flow this follow-up leaves through; '' when it has none (and then it stays due)."""
    entry = _decided().get(template_key)
    if entry is not None:
        return entry['flow']
    if template_key not in FLOW_NAMES:
        return ''
    return (found(now).get(template_key) or {}).get('flow', '')
