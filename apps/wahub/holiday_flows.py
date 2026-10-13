"""
The holidays board of a branch — a ManyChat automation the owner made, which
sends a picture of every date that matters there.

The owner, 13.10.2026: "lessons in Hanukkah and on holidays, or anything to do
with it — send the automation kogo_Holidays (a picture of all the relevant
dates is attached there). Check that it exists and works well. It is only for
the branches מינץ, דימרי סנטר, קרל וגרטי קורי, אם המושבות, שוהם. יהוד's is
kogo_Holidays_yahud, אור יהודה's kogo_Holidays_oryehuda, and
כפר גנים / אויערבך / זמיר's kogo_Holidays_zamir."

BOARDS is that sentence as data, and the one place it is written: which
automation, by its fixed name in ManyChat, serves which branches. A branch is
recognised by its own name as Kogo spells it ("מרכז העיר - מינץ 24",
"מרכז זמיר - אויערבך משה 3", "קרל וגרטי קורי 8") and, where a whole city is
one branch, by its city. A branch that is on nobody's list — the second branch
of a city, a new one — has no board: the bot says it is asking the office.

Three things are answered here, and nothing is sent:

    board_of(branch)   the board a branch's holidays are on, or None.
    found()            which of the boards' automations ManyChat really has, by
                       their names. Kept in the app's own table and asked for
                       again once in FRESH_FOR — the serverless instances share
                       no other cache — exactly as lead_templates.py keeps the
                       follow-ups' automations, and through the same asking
                       (lead_templates.manychat_flows): capital letters do not
                       matter, nor whether the words are joined by "_", "-" or
                       a space; two automations under one name are nobody's.
                       The bot's tool reads what is kept and never asks
                       (`ask=False`): a tool neither writes nor reaches out.
                       The cron and the status screen do the asking.
    status()           the two together, for GET status/ — what whoever checks
                       "does it exist" reads.

The sending itself is the live test's (live_test.py → sending.send_new_bot_flow),
from what the answer's own tool call says (shadow.flows_to_send).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from apps.wahub import lead_templates, settings_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Board:
    flow: str               # the automation's fixed name in ManyChat
    label: str              # what the office reads in the conversation and on the screen
    branches: str           # a pattern over a branch's own name, as Kogo or a parent spells it
    cities: tuple = ()      # cities every branch of which is on this board


BOARDS = (
    Board('kogo_Holidays', 'לוח החופשות', r'מינץ|דימרי|דמרי|כפר סבא הירוקה|קרל וגרטי|גרטי קורי|אם המושבות|רפאל איתן|שוהם', cities=('שוהם',)),
    Board('kogo_Holidays_yahud', 'לוח החופשות של יהוד', r'יהוד(?!ה)', cities=('יהוד', 'יהוד מונוסון')),
    Board('kogo_Holidays_oryehuda', 'לוח החופשות של אור יהודה', r'אור יהודה', cities=('אור יהודה',)),
    Board('kogo_Holidays_zamir', 'לוח החופשות של מרכז זמיר', r'כפר גנים|אויערבך|זמיר'),
)
BY_FLOW = {board.flow: board for board in BOARDS}

# The sentence the bot says beside the board: one short line, and the picture follows it.
SAY = 'שולחת לכם את לוח החופשות של הסניף, עם כל התאריכים'

NOT_ASKED_YET = 'עוד לא נבדק מול ManyChat'
NOT_IN_MANYCHAT = 'לא נמצאה ב-ManyChat אוטומציה בשם הזה'
TWICE_IN_MANYCHAT = 'ב-ManyChat יש יותר מאוטומציה אחת בשם הזה'


def _plain(text) -> str:
    return re.sub(r'[\s\-"״\'׳]', '', str(text or '')).casefold()


def board_of(branch) -> Board | None:
    """
    The board a branch's holidays are on. By the branch's own name first — a
    city may hold branches of two boards (מינץ and כפר גנים are both in
    פתח תקווה) — and by its city where the name says nothing. A name two boards
    claim is nobody's.
    """
    name = str(getattr(branch, 'name', '') or '')
    named = [board for board in BOARDS if re.search(board.branches, name)]
    if named:
        return named[0] if len(named) == 1 else None
    city = _plain(branch.city.name) if getattr(branch, 'city_id', None) else ''
    in_city = [board for board in BOARDS if city and city in {_plain(known) for known in board.cities}]
    return in_city[0] if len(in_city) == 1 else None


def _wanted() -> bool:
    """Is there a branch a board would be sent for? Without one there is nothing to ask ManyChat about."""
    from apps.core.models import Branch

    return any(board_of(branch) is not None for branch in Branch.objects.filter(is_active=True).select_related('city'))


def by_name(flows: list[dict]) -> tuple[dict, list]:
    """({board's flow name: {flow, name}} for every board exactly one automation is named after, the names two or more carry)."""
    named: dict = {}
    for flow in flows:
        named.setdefault(lead_templates.same_name(flow['name']), []).append(flow)
    found, twice = {}, []
    for board in BOARDS:
        carrying = named.get(lead_templates.same_name(board.flow)) or []
        if len(carrying) == 1:
            found[board.flow] = {'flow': carrying[0]['id'], 'name': carrying[0]['name']}
        elif carrying:
            twice.append(board.flow)
            logger.warning('wahub holiday boards: %d automations are named %s; it is left unconnected', len(carrying), board.flow)
    return found, twice


def remember(flows: list[dict], now=None) -> dict:
    """Keep what ManyChat's list says about the boards' names, with the moment it said so."""
    found_now, twice = by_name(flows)
    settings_store.put(settings_store.HOLIDAY_FLOWS_FOUND, {
        'at': (now or timezone.now()).isoformat(), 'ok': True, 'flows': found_now, 'twice': twice,
    })
    return found_now


def _kept() -> dict:
    """What is kept, cleaned: {flows: {name: {flow, name}}, twice: [...], at: datetime | None, ok: bool}."""
    kept = settings_store.get(settings_store.HOLIDAY_FLOWS_FOUND, {})
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
            for key, row in flows.items() if key in BY_FLOW and isinstance(row, dict) and str(row.get('flow') or '').strip()
        },
        'twice': [name for name in kept.get('twice') or [] if name in BY_FLOW] if isinstance(kept.get('twice'), list) else [],
        'at': at,
        'ok': bool(kept.get('ok')),
    }


def _ask_manychat() -> bool:
    """Is there a ManyChat to ask, and a reason to ask it."""
    from apps.core.manychat_service import ManyChatService
    from apps.wahub import sending

    return not sending.simulate_send() and ManyChatService().is_configured and _wanted()


def found(now=None, *, ask: bool = True) -> dict:
    """
    {board's flow name: {flow, name}} — the automations found in ManyChat by
    the boards' names. The kept answer while it is fresh; otherwise ManyChat is
    asked once (when `ask` allows, there is a ManyChat to ask and some branch
    has a board) and the answer kept. When it does not answer, the last answer
    stands and the next asking waits its turn.
    """
    now = now or timezone.now()
    kept = _kept()
    if not ask or (kept['at'] is not None and abs(now - kept['at']) < lead_templates.FRESH_FOR):
        return kept['flows']
    if not _ask_manychat():
        return kept['flows']
    flows, _why = lead_templates.manychat_flows(timeout=lead_templates.ASK_SECONDS)
    if flows is None:
        settings_store.put(settings_store.HOLIDAY_FLOWS_FOUND, {
            'at': now.isoformat(), 'ok': False, 'flows': kept['flows'], 'twice': kept['twice'],
        })
        return kept['flows']
    return remember(flows, now)


def flow_of(board: Board) -> dict | None:
    """{name, ns, label} of the board's automation as ManyChat was last heard to have it; None when it has none. Never asks."""
    row = found(ask=False).get(board.flow)
    if not row:
        return None
    return {'name': board.flow, 'ns': row['flow'], 'label': board.label}


def why_missing(board: Board) -> str:
    """Why a board's automation cannot be sent, in the office's words."""
    kept = _kept()
    if board.flow in kept['twice']:
        return TWICE_IN_MANYCHAT
    return NOT_ASKED_YET if kept['at'] is None else NOT_IN_MANYCHAT


def status(now=None, *, ask: bool = True) -> dict:
    """
    GET status/ → holiday_flows: for each board — the automation's name, whether
    ManyChat has it, and the branches of Kogo it is sent for; the names that
    are missing; the branches that are on no board; and when ManyChat was last
    asked. Reads; the one thing it may write is the kept answer, when it is stale.
    """
    from apps.core.models import Branch

    try:
        found_now = found(now, ask=ask)
    except Exception:  # the status is what the whole screen opens with: a fault in the asking is not its fault
        logger.exception('wahub holiday boards: ManyChat could not be asked; the status shows what is kept')
        found_now = found(now, ask=False)
    kept = _kept()
    on_board: dict = {board.flow: [] for board in BOARDS}
    without = []
    # Sorted here and not by the database: its collation decides how Hebrew sorts.
    for branch in sorted(Branch.objects.filter(is_active=True).select_related('city'), key=lambda row: (row.name, str(row.id))):
        board = board_of(branch)
        if board is None:
            without.append(branch.name)
        else:
            on_board[board.flow].append(branch.name)
    rows = []
    for board in BOARDS:
        there = board.flow in found_now
        rows.append({
            'name': board.flow,
            'label': board.label,
            'found': there,
            'problem': '' if there else why_missing(board),
            'branches': on_board[board.flow],
        })
    return {
        'asked_at': kept['at'].isoformat() if kept['at'] else None,
        'manychat_answered': kept['ok'],
        'flows': rows,
        'missing': [row['name'] for row in rows if not row['found']],
        'branches_without_a_board': without,
    }
