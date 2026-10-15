"""
What the office sends by a press, and what each says (owner, 15.10.2026).

Three things live here, and nothing here sends anything by itself:

    the two openers   "פתיחת שיחה" and "המשך שיחה" — the only two buttons in a
                      conversation WhatsApp's 24 hours have closed (or that the
                      customer never opened). Each is a ManyChat automation the
                      owner makes once, under a fixed name: `open chat` and
                      `chat continue` (docs/WAHUB-LEAD-TEMPLATES-FOR-OWNER.md,
                      templates 22 and 23). Found by that name the way the
                      follow-ups' templates are (lead_templates.py): capital
                      letters do not matter, nor spaces, "_" or "-" between the
                      words (`Open Chat` = `open_chat` = `openchat`); two
                      automations under one name are nobody's. What ManyChat
                      has is kept in the app's own table and asked for again
                      once in lead_templates.FRESH_FOR — on a press, never more
                      than once — and a name the last answer did not have is
                      asked for at once: the owner may have just made it.
    what a template   the line the conversation keeps for a template is its
    says              words, not the automation's name: an opener's own words
                      with the customer's first name, and for one of the
                      follow-ups' templates (lead_templates.FLOW_NAMES) its
                      phrasing without the variables — the fixed sentence the
                      approved template carries.
    the goodbye       ✓ "סגירת שיחה" first sends the ready-made reply of that
                      title, when the 24 hours are open and sending is on, and
                      only then closes (views.ContactViewSet.close).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from apps.wahub import lead_followup_seed, lead_followups, lead_templates, sending, settings_store
from apps.wahub.models import DELIVERED_STATUSES, DIRECTION_OUT, Contact, Message, QuickReply

logger = logging.getLogger(__name__)

OPEN = 'open'
CONTINUE = 'continue'


@dataclass(frozen=True)
class Opener:
    kind: str       # what the screen sends: 'open' | 'continue'
    key: str        # its key, as lead_templates names the follow-ups' templates
    flow: str       # the automation's fixed name in ManyChat
    label: str      # the button
    words: str      # the template's words, {{first_name}} filled per contact


OPENERS = (
    Opener(
        OPEN, 'open_chat', 'open chat', 'פתיחת שיחה',
        'היי {{first_name}} 🙂 כאן סטודיו קוגומלו. חזרנו אליכם בהמשך לפנייה שלכם, אפשר לענות כאן ונמשיך מכאן',
    ),
    Opener(
        CONTINUE, 'chat_continue', 'chat continue', 'המשך שיחה',
        'היי {{first_name}} 🙂 ממשיכים את השיחה שלנו מקוגומלו. אפשר לענות להודעה הזאת ונמשיך מכאן',
    ),
)
BY_KIND = {opener.kind: opener for opener in OPENERS}
BY_KEY = {opener.key: opener for opener in OPENERS}

# What ManyChat has under the two names, with the moment it said so. A row of the app's own
# settings table (a key, no column of its own): no migration.
FOUND_KEY = 'chat_openers_found'

# Why an opener cannot leave (the view says it in words).
MISSING = 'missing'
TWICE = 'twice'

GOODBYE_TITLE = 'סגירת שיחה'
GOODBYE_SENT = 'sent'
GOODBYE_SKIPPED = 'skipped'
GOODBYE_FAILED = 'failed'

_FIRST_NAME = re.compile(r'\{\{\s*first_name\s*\}\}')
_FIRST_NAME_WITH_SPACE = re.compile(r'[ \t]?\{\{\s*first_name\s*\}\}')


def fill_first_name(text: str, name: str) -> str:
    """
    {{first_name}} filled with the contact's first word, as the screen fills a
    ready-made reply (fillQuickReply). No name: the sentence without it and
    without the space before it — "היי {{first_name}} 🙂" becomes "היי 🙂".
    """
    words = str(name or '').split()
    if words:
        return _FIRST_NAME.sub(lambda _match: words[0], text or '')
    return re.sub(r'^[\s,،]+', '', _FIRST_NAME_WITH_SPACE.sub('', text or ''))


def same_name(name: str) -> str:
    """A name as it is compared: no capitals, and no space, "_" or "-" anywhere."""
    return re.sub(r'[\s_\-]+', '', str(name or '').strip().casefold())


# --- the two openers -------------------------------------------------------------------------------

def words(opener: Opener, contact: Contact) -> str:
    return fill_first_name(opener.words, contact.name)[:sending.MAX_TEXT_CHARS]


def by_name(flows: list[dict]) -> tuple[dict, list]:
    """({opener key: {flow, name}} for each opener exactly one automation carries the name of, the keys two or more carry)."""
    named: dict = {}
    for flow in flows:
        named.setdefault(same_name(flow['name']), []).append(flow)
    found, twice = {}, []
    for opener in OPENERS:
        carrying = named.get(same_name(opener.flow)) or []
        if len(carrying) == 1:
            found[opener.key] = {'flow': carrying[0]['id'], 'name': carrying[0]['name']}
        elif carrying:
            twice.append(opener.key)
            logger.warning('wahub openers: %d automations are named %s; it is left unconnected', len(carrying), opener.flow)
    return found, twice


def remember(flows: list[dict], now=None) -> dict:
    found, twice = by_name(flows)
    settings_store.put(FOUND_KEY, {'at': (now or timezone.now()).isoformat(), 'flows': found, 'twice': twice})
    return {'flows': found, 'twice': twice}


def _kept() -> dict:
    kept = settings_store.get(FOUND_KEY, {})
    kept = kept if isinstance(kept, dict) else {}
    flows = kept.get('flows') if isinstance(kept.get('flows'), dict) else {}
    try:
        at = datetime.fromisoformat(kept.get('at') or '')
        at = at if timezone.is_aware(at) else timezone.make_aware(at)
    except (TypeError, ValueError):
        at = None
    return {
        'flows': {
            str(key): {'flow': str(row.get('flow') or '').strip(), 'name': str(row.get('name') or '').strip()}
            for key, row in flows.items() if key in BY_KEY and isinstance(row, dict) and str(row.get('flow') or '').strip()
        },
        'twice': [key for key in kept.get('twice') or [] if key in BY_KEY] if isinstance(kept.get('twice'), list) else [],
        'at': at,
    }


def flow_for(opener: Opener, now=None) -> tuple[str, str]:
    """
    (the flow the opener leaves through, '') — or ('', why): MISSING, TWICE,
    or why ManyChat cannot be asked (lead_templates.manychat_flows). The kept
    answer while it is fresh and has the flow; otherwise ManyChat is asked now,
    once. When it does not answer, a flow it was last heard to have stands.
    """
    now = now or timezone.now()
    kept = _kept()
    row = kept['flows'].get(opener.key)
    if row and kept['at'] is not None and abs(now - kept['at']) < lead_templates.FRESH_FOR:
        return row['flow'], ''
    flows, why = lead_templates.manychat_flows(timeout=lead_templates.ASK_SECONDS)
    if flows is None:
        return (row['flow'], '') if row else ('', why)
    found = remember(flows, now)
    row = found['flows'].get(opener.key)
    if row:
        return row['flow'], ''
    return '', TWICE if opener.key in found['twice'] else MISSING


def missing_detail(opener: Opener, why: str) -> str:
    if why == MISSING:
        return f'חסרה ב-ManyChat תבנית בשם {opener.flow}'
    if why == TWICE:
        return f'ב-ManyChat יש יותר מאוטומציה אחת בשם {opener.flow}. משאירים אחת.'
    return f'אי אפשר לבדוק עכשיו ב-ManyChat שהתבנית {opener.flow} קיימת ({why}). לא נשלח כלום.'


# --- what a template says --------------------------------------------------------------------------

def lead_template_words(template_key: str) -> str:
    """
    The fixed sentence of one of the follow-ups' templates: its phrasing as it
    stands, without the variables (lead_followup_seed — "the same sentence
    without the variables"). '' for a key that has no live phrasing.
    """
    for kind, variant, *_rest in lead_followup_seed.PHRASINGS:
        prefix = f'{kind}_{variant}_' if variant else f'{kind}_'
        step = template_key[len(prefix):] if template_key.startswith(prefix) else ''
        if not step.isdigit():
            continue
        body, key = lead_followups.phrasing(kind, int(step), variant)
        if body and key == template_key:
            return lead_followups.render(body, {})
    return ''


def words_for_flow(flow_ns: str, contact: Contact) -> str:
    """
    What a template the office sends through "שלח תבנית" says, when Kogo knows
    it: one of the two openers, or one of the follow-ups' templates. '' for any
    other automation (the conversation then shows its name, as before). Reads
    only what is kept — ManyChat is not asked.
    """
    flow_ns = (flow_ns or '').strip()
    if not flow_ns:
        return ''
    for key, row in _kept()['flows'].items():
        if row['flow'] == flow_ns:
            return words(BY_KEY[key], contact)
    for key, row in sorted(lead_templates.connections(ask=False).items()):
        if (row.get('flow') or '') == flow_ns:
            said = lead_template_words(key)
            if said:
                return said[:sending.MAX_TEXT_CHARS]
    return ''


# --- ✓ "סגירת שיחה" -------------------------------------------------------------------------------

def goodbye_reply() -> QuickReply | None:
    """The ready-made reply titled "סגירת שיחה" (its title trimmed), the first one written."""
    for reply in QuickReply.objects.filter(title__contains=GOODBYE_TITLE).order_by('id'):
        if reply.title.strip() == GOODBYE_TITLE and reply.text.strip():
            return reply
    return None


def _already_said(contact: Contact, text: str) -> bool:
    """The newest line of the conversation is this very goodbye, delivered: a second ✓ does not say it twice."""
    last = Message.objects.filter(contact=contact).order_by('-id').only('direction', 'status', 'text').first()
    return (
        last is not None and last.direction == DIRECTION_OUT and last.status in DELIVERED_STATUSES
        and last.text.strip() == text.strip()
    )


def say_goodbye(contact: Contact, user) -> tuple[str, str]:
    """
    Before ✓ closes the conversation: the ready-made reply "סגירת שיחה", sent as
    the person who pressed — when there is one, the 24 hours are open and a
    message may leave. Returns (sent | skipped | failed, why in Hebrew; '' when
    there is nothing to say). Never raises: the conversation is closed whatever
    becomes of the goodbye.
    """
    reply = goodbye_reply()
    if reply is None:
        return GOODBYE_SKIPPED, ''
    if contact.is_demo:
        return GOODBYE_SKIPPED, sending.DEMO_CONTACT
    if not sending.can_free_text(contact):
        return GOODBYE_SKIPPED, 'עברו 24 שעות מההודעה האחרונה של הלקוח, ווואטסאפ לא מאפשר לשלוח לו הודעה חופשית.'
    if not sending.sending_enabled(contact):
        return GOODBYE_SKIPPED, 'השליחה ללקוחות כבויה.'
    text = fill_first_name(reply.text.strip(), contact.name)[:sending.MAX_TEXT_CHARS]
    if _already_said(contact, text):
        return GOODBYE_SKIPPED, 'הודעת הסגירה כבר נשלחה בשיחה הזו.'
    try:
        message = sending.send_text(contact, text, user)
    except sending.WindowClosed:
        return GOODBYE_SKIPPED, 'עברו 24 שעות מההודעה האחרונה של הלקוח, ווואטסאפ לא מאפשר לשלוח לו הודעה חופשית.'
    except Exception:  # the goodbye never stops the close
        logger.exception('wahub goodbye to contact %s failed unexpectedly', contact.pk)
        return GOODBYE_FAILED, 'תקלה לא צפויה בשליחה. לא ידוע אם ההודעה נשלחה.'
    if message.status in DELIVERED_STATUSES:
        return GOODBYE_SENT, ''
    return GOODBYE_FAILED, message.error or 'ההודעה לא נשלחה.'
