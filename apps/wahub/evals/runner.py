"""
Playing a written conversation against the bot, and keeping the run.

A turn is played in memory: the customer's messages of the script become the
messages to answer, the turns before it become the history, and the bot drafts
its answer exactly as it does for a real conversation (`shadow.draft`) — the
same instructions, the same knowledge, the same tools on the same data. No
contact, message or shadow reply is made, nothing is sent, and nothing in a
real conversation is read. What is kept is the BotEvalRun row.

Where there is a key the bot is Claude, and a failure of Claude is not turned
into a rule-built answer: a turn it did not answer is tried again, and a run
that still has no answer is "לא רץ", not passed and not failed. On a machine
with no key the stub answers, and the run says so (`engine: "stub"`) — that
checks the scripts and the tools, not the bot.

A run may take several requests. A server function is cut after about a
minute, and a conversation of four turns with the judge after it does not fit:
`advance` plays as many turns as the time it was given allows, saves, and says
`running`; the next request continues from the row.
"""
from __future__ import annotations

import logging
import time as time_module
from datetime import datetime, timedelta

from django.utils import timezone

from apps.wahub import shadow, shadow_tools
from apps.wahub.evals import checks, judge, rules, scenarios, world
from apps.wahub.models import (
    EVAL_ERROR,
    EVAL_FAILED,
    EVAL_FINAL_STATUSES,
    EVAL_PASSED,
    EVAL_RUNNING,
    EVAL_SKIPPED,
    EVAL_STATUS_CHOICES,
    SHADOW_MODEL_STUB,
    TYPE_IMAGE,
    TYPE_VOICE,
    BotEvalRun,
)

logger = logging.getLogger(__name__)

# A turn is not begun with less than this left of the request, nor the judge with less than that.
MIN_TURN_SECONDS = 22
MIN_JUDGE_SECONDS = 12
# How many times a turn the model did not answer is tried again before the run is given up.
MAX_ATTEMPTS = 3
# A run nobody continued for this long is no longer "in progress".
STALE_AFTER = timedelta(minutes=10)
BETWEEN_TURNS = timedelta(minutes=1)

STATUS_LABELS = dict(EVAL_STATUS_CHOICES)
CHECK_LABELS = {'passed': 'עבר', 'failed': 'נכשל', 'not_judged': 'לא נשפט'}
KINDS = {scenarios.VOICE: TYPE_VOICE, scenarios.IMAGE: TYPE_IMAGE}


class EvalError(ValueError):
    """A request the runner refuses, with the reason in Hebrew."""


def engine() -> dict:
    """Who answers a run started now."""
    configured = shadow.shadow_configured()
    return {
        'configured': configured,
        'model': shadow.model_name() if configured else SHADOW_MODEL_STUB,
        'judge': judge.configured(),
        'label': 'הבוט עצמו' if configured else 'בוט מדומה (אין מפתח של Claude במערכת הזאת)',
    }


# --- the script, filled -----------------------------------------------------------------------------------------

def _fill(text: str, words: dict) -> str:
    return text.format(**words) if '{' in text else text


def _messages(turn, words: dict) -> list:
    rows = []
    for said in turn.says:
        if isinstance(said, scenarios.Msg):
            rows.append({'kind': said.kind, 'text': _fill(said.text, words)})
        else:
            rows.append({'kind': 'text', 'text': _fill(said, words)})
    return rows


def _missing(scenario, built: dict) -> list:
    """Why the script cannot run on this system: the pieces of the world it needs and the system does not hold."""
    reasons = []
    missing = built.get('missing') or {}
    if missing.get('knowledge'):
        reasons.append(missing['knowledge'])
    for key in scenario.needs:
        if key not in (built.get('words') or {}):
            reasons.append(missing.get(key) or missing.get('stage') or f'חסר במערכת: {key}')
    for turn in scenario.turns:
        for check in turn.expect:
            head, _, path = check.id.partition(':')
            if head in ('link', 'no_link') and missing.get(f'link_{path}'):
                reasons.append(missing[f'link_{path}'])
    return list(dict.fromkeys(reasons))


def _moments_of(scenario, clocks: dict) -> list:
    """When each turn of the script is written: the script's clock, a minute between turns, and its silences."""
    moment = datetime.fromisoformat(clocks[scenario.clock])
    when = []
    for index, turn in enumerate(scenario.turns):
        if index:
            moment = moment + (timedelta(hours=turn.after_hours) if turn.after_hours else BETWEEN_TURNS)
        when.append(moment)
    return when


def _history(scenario, run: BotEvalRun, upto: int, words: dict, when: list) -> list:
    """What was said before turn `upto`: the script's earlier conversation, then the turns already played."""
    lines = []
    for who, text, hours_ago in scenario.before:
        lines.append({
            'who': 'customer' if who == 'customer' else who, 'text': _fill(text, words),
            # A line of the bot's in an earlier conversation is the new bot's own.
            'new_bot': who == 'bot', 'sent_at': when[0] - timedelta(hours=hours_ago),
        })
    for index in range(upto):
        played = run.turns[index]
        for message in played['customer']:
            lines.append({
                'who': 'customer', 'text': message['text'], 'sent_at': when[index],
                'message_type': KINDS.get(message['kind'], 'text'),
            })
        lines.append({'who': 'bot', 'new_bot': True, 'text': played['reply'], 'sent_at': when[index] + timedelta(seconds=20)})
    return lines


def _customer(scenario, built: dict, today) -> dict | None:
    """
    The card of the invented family a script is written by, in the shape of the
    `customer_card` tool: a child registered to — or booked for a trial in —
    the class the script is about. Nobody's: the names are made up.
    """
    if not scenario.customer:
        return None
    main = (built.get('facts') or {}).get('main') or {}
    lesson = (main.get('lessons') or [{}])[0]
    where = {
        'course_id': main.get('id', ''), 'course': main.get('name', ''), 'course_type': main.get('field', ''),
        'branch': main.get('branch', ''), 'city': main.get('city', ''), 'day': lesson.get('day', ''),
        'start': lesson.get('start', ''), 'end': lesson.get('end', ''), 'instructor': lesson.get('instructor', ''),
    }
    booked = scenario.customer == 'trial_booked'
    if booked:
        where['trial_on'] = today.strftime('%d.%m.%Y')
    return {
        'known': True,
        'name': 'רותם בדויה',
        'outcome': 'trial_upcoming' if booked else 'customer_before',
        'outcome_label': 'נרשם לשיעור ניסיון' if booked else 'לקוח רשום',
        'detail': '',
        'children': [{
            'id': 'script-child', 'name': 'נגה בדויה',
            'status': 'registered_for_trial' if booked else 'active', 'status_label': 'נרשם לניסיון' if booked else 'פעיל',
            'classes': [where],
        }],
        'trial': {'on': where['trial_on'], 'course': where['course'], 'branch': where['branch'], 'paid': True} if booked else {},
        'what_we_know': {'topic': '', 'course_type': '', 'city': '', 'branch': '', 'child_age': '', 'summary': ''},
        'handled_by': 'bot',
        'needs_human': False,
    }


def _knowledge_text(items) -> str:
    """What the knowledge lets the bot say: the bodies and the data. Not the examples — a "wrong" example holds what must not be said."""
    def strings(value):
        if isinstance(value, dict):
            for inner in value.values():
                yield from strings(inner)
        elif isinstance(value, (list, tuple)):
            for inner in value:
                yield from strings(inner)
        elif isinstance(value, str):
            yield value

    parts = []
    for item in items:
        parts.append(item.body or '')
        parts.extend(strings(item.data or {}))
    return '\n'.join(part for part in parts if part)


def _office_days() -> tuple:
    from apps.wahub import knowledge

    item = knowledge.office_hours_item()
    weekly = (item.data or {}).get('weekly') if item else None
    if not isinstance(weekly, dict):
        return ()
    return tuple(knowledge.DAY_LABELS[day] for day in knowledge.DAYS if (weekly.get(day) or {}).get('open'))


# --- one turn ------------------------------------------------------------------------------------------------------

def _play(run: BotEvalRun, scenario, index: int, catalog, deadline: float | None) -> dict:
    """The bot's answer to turn `index`, with its checks. Raises ShadowModelError when the model gave none."""
    words = run.world.get('words') or {}
    turn = scenario.turns[index]
    when = _moments_of(scenario, run.world['clocks'])
    messages = _messages(turn, words)
    targets = [
        shadow.Target(id=None, text=message['text'], message_type=KINDS.get(message['kind'], 'text'), sent_at=when[index])
        for message in messages
    ]
    last_outbound = _fill(turn.last_outbound, words) if turn.last_outbound else None
    ctx = shadow.scripted_context(
        _history(scenario, run, index, words, when), targets, now=when[index], last_outbound=last_outbound,
        customer=_customer(scenario, run.world, timezone.localtime(when[index]).date()),
    )
    # With a key, the answer is Claude's or there is none: a rule-built line in its place would be judged as the bot's.
    result = shadow.draft(ctx, deadline=deadline, fallback=False)
    calls = list(ctx.tool_ctx.calls)

    earlier_calls = [
        {'name': tool['name'], 'input': tool.get('input') or {}, 'result': tool.get('result')}
        for played in run.turns[:index] for tool in played.get('tools') or []
    ]
    written = [message['text'] for played in run.turns[:index] for message in played['customer']] + [message['text'] for message in messages]
    before = [_fill(text, words) for who, text, hours_ago in scenario.before if who == 'customer']
    recent = [
        _fill(text, words) for who, text, hours_ago in scenario.before
        if who == 'customer' and timedelta(hours=hours_ago) < shadow.SESSION_GAP
    ]
    moment = checks.Moment(
        reply=result.text, index=index, customer=[message['text'] for message in messages],
        customer_all='\n'.join(before + written), customer_now='\n'.join(recent + written),
        calls=calls, earlier_calls=earlier_calls,
        previous_reply=run.turns[index - 1]['reply'] if index else '',
        request_human=result.request_human, tags=list(ctx.tool_ctx.tags), world=run.world,
        knowledge_text=_knowledge_text(ctx.items), office_days=_office_days(), catalog=catalog, allow=turn.allow,
    )
    verdicts = checks.run(checks.GENERAL + tuple(turn.expect), moment, turn=index + 1)
    return {
        'turn': index + 1,
        'at': when[index].isoformat(),
        'customer': messages,
        'last_outbound': last_outbound or '',
        'reply': result.text,
        'reasoning': result.reasoning,
        'model': result.model,
        'took_ms': result.took_ms,
        'request_human': result.request_human,
        'request_human_reason': result.request_human_reason,
        'tags': list(ctx.tool_ctx.tags),
        'tools': [
            {
                'name': call['name'], 'input': call['input'],
                'summary': shadow_tools.summarize_result(call['name'], call['result']), 'result': call['result'],
            }
            for call in calls
        ],
        'checks': verdicts,
    }


# --- a run -----------------------------------------------------------------------------------------------------------

def start(scenario_id: str, user=None, *, now=None) -> BotEvalRun:
    """A new run of one script: its words and facts read from the system now. Skipped at once when the system cannot fill it."""
    scenario = scenarios.BY_ID.get(str(scenario_id or ''))
    if scenario is None:
        raise EvalError('אין תרחיש כזה.')
    built = world.build(now)
    run = BotEvalRun(
        scenario_id=scenario.id, status=EVAL_RUNNING, world=built, engine=engine()['model'],
        created_by=user if getattr(user, 'is_authenticated', False) else None,
    )
    reasons = _missing(scenario, built)
    if reasons:
        run.status, run.detail, run.finished_at = EVAL_SKIPPED, 'אי אפשר להריץ כאן: ' + '; '.join(reasons), timezone.now()
    run.save()
    return run


def _finish(run: BotEvalRun, status: str, detail: str = '') -> BotEvalRun:
    run.status, run.finished_at = status, timezone.now()
    if detail:
        run.detail = detail
    run.took_ms = sum(int(played.get('took_ms') or 0) for played in run.turns)
    run.save()
    return run


def _rubric(scenario) -> list:
    return [
        {'turn': index + 1, 'number': number, 'text': sentence}
        for index, turn in enumerate(scenario.turns) for number, sentence in enumerate(turn.rubric, 1)
    ]


def advance(run: BotEvalRun, *, deadline: float | None = None, with_judge: bool = True) -> BotEvalRun:
    """
    Plays the turns that are left while the time allows, then the judge, then
    the verdict. Returns the run: still `running` when the time ran out first —
    call again with the same run.
    """
    if run.status != EVAL_RUNNING:
        return run
    scenario = scenarios.BY_ID.get(run.scenario_id)
    if scenario is None:
        return _finish(run, EVAL_ERROR, 'התרחיש הזה כבר לא קיים.')

    def left() -> float:
        return float('inf') if deadline is None else deadline - time_module.monotonic()

    catalog = world.Catalog()
    played_now = 0
    while len(run.turns) < len(scenario.turns):
        if played_now and left() < MIN_TURN_SECONDS:
            return run
        try:
            record = _play(run, scenario, len(run.turns), catalog, deadline)
        except shadow.ShadowModelError as exc:
            run.attempts += 1
            if run.attempts >= MAX_ATTEMPTS:
                return _finish(run, EVAL_ERROR, f'הבוט לא ענה בתור {len(run.turns) + 1}: {exc}')
            run.save(update_fields=['attempts'])
            return run
        run.checks = list(run.checks) + record.pop('checks')
        run.turns = list(run.turns) + [record]
        run.engine = record['model'] or run.engine
        run.save(update_fields=['turns', 'checks', 'engine'])
        played_now += 1

    sentences = _rubric(scenario)
    if sentences and not any(row['kind'] == 'rubric' for row in run.checks):
        if with_judge and judge.configured():
            if left() < MIN_JUDGE_SECONDS:
                return run
            verdicts = judge.judge(run.turns, sentences, deadline=deadline)
        else:
            reason = judge.NO_KEY if with_judge else 'לא נשפט: הריצה הזאת בלי שיפוט'
            verdicts = [{'status': 'not_judged', 'explain': reason} for _ in sentences]
        run.checks = list(run.checks) + [
            {
                'id': f'rubric_{row["turn"]}_{row["number"]}', 'turn': row['turn'], 'kind': 'rubric', 'title': row['text'],
                'status': verdict['status'], 'explain': verdict['explain'],
            }
            for row, verdict in zip(sentences, verdicts)
        ]
    failed = any(row['status'] == 'failed' for row in run.checks)
    return _finish(run, EVAL_FAILED if failed else EVAL_PASSED)


def run_to_end(scenario_id: str, user=None, *, with_judge: bool = True, now=None) -> BotEvalRun:
    """One script from its first turn to its verdict, with no clock on it — the management command's way."""
    run = start(scenario_id, user, now=now)
    while run.status == EVAL_RUNNING:
        run = advance(run, with_judge=with_judge)
    return run


def continue_or_start(scenario_id, run_id, user=None, *, deadline: float | None = None) -> BotEvalRun:
    """POST bot/evals/run/: a new run of the script, or the next stretch of the one named."""
    if run_id in (None, ''):
        run = start(scenario_id, user)
    else:
        run = BotEvalRun.objects.filter(pk=run_id).first() if str(run_id).isdigit() else None
        if run is None:
            raise EvalError('הריצה לא נמצאה.')
        if scenario_id and run.scenario_id != scenario_id:
            raise EvalError('הריצה הזאת שייכת לתרחיש אחר.')
    return advance(run, deadline=deadline)


# --- the JSON of the contract ---------------------------------------------------------------------------------------

def _rule_refs(ids) -> list:
    return [{'id': rule_id, 'title': rules.RULES.get(rule_id, '')} for rule_id in ids]


def _counts(rows: list) -> dict:
    return {status: sum(1 for row in rows if row['status'] == status) for status in ('passed', 'failed', 'not_judged')}


def _headline(run: BotEvalRun) -> str:
    """The one line a list shows under a run that did not pass."""
    if run.status in (EVAL_SKIPPED, EVAL_ERROR):
        return run.detail
    failed = next((row for row in run.checks if row['status'] == 'failed'), None)
    return failed['explain'] if failed else ''


def _scenario_head(scenario) -> dict:
    return {
        'id': scenario.id,
        'title': scenario.title,
        'group': scenario.group,
        'group_title': scenarios.GROUP_TITLES.get(scenario.group, scenario.group),
        'what': scenario.what,
        'rules': _rule_refs(scenario.rules),
        'turns': len(scenario.turns),
    }


def run_payload(run: BotEvalRun) -> dict:
    """One run in full: the conversation as it was played, every check, and each failure beside the answer it is about."""
    scenario = scenarios.BY_ID.get(run.scenario_id)
    head = _scenario_head(scenario) if scenario else {
        'id': run.scenario_id, 'title': run.scenario_id, 'group': '', 'group_title': '', 'what': '', 'rules': [], 'turns': len(run.turns),
    }
    by_turn = {played['turn']: played for played in run.turns}
    rows = [{**row, 'status_label': CHECK_LABELS.get(row['status'], row['status'])} for row in run.checks]
    return {
        'run_id': run.id,
        'scenario': head,
        'status': run.status,
        'status_label': STATUS_LABELS.get(run.status, run.status),
        'done': run.status in EVAL_FINAL_STATUSES,
        'turns_played': len(run.turns),
        'turns_total': head['turns'],
        'engine': run.engine,
        'engine_is_stub': run.engine == SHADOW_MODEL_STUB,
        'started_at': run.created_at.isoformat() if run.created_at else None,
        'finished_at': run.finished_at.isoformat() if run.finished_at else None,
        'took_ms': run.took_ms or sum(int(played.get('took_ms') or 0) for played in run.turns),
        'detail': run.detail,
        'headline': _headline(run),
        'counts': _counts(rows),
        'conversation': [
            {
                'turn': played['turn'],
                'customer': played['customer'],
                'last_outbound': played.get('last_outbound') or '',
                'bot': played['reply'],
                'reasoning': played.get('reasoning') or '',
                # What the tools returned stays on the row: the screen shows the line, not the data.
                'tools': [{'name': tool['name'], 'input': tool['input'], 'summary': tool['summary']} for tool in played.get('tools') or []],
                'request_human': bool(played.get('request_human')),
                'request_human_reason': played.get('request_human_reason') or '',
                'tags': played.get('tags') or [],
            }
            for played in run.turns
        ],
        'checks': rows,
        'failures': [
            {
                **row,
                'customer': (by_turn.get(row['turn']) or {}).get('customer') or [],
                'bot': (by_turn.get(row['turn']) or {}).get('reply') or '',
            }
            for row in rows if row['status'] == 'failed'
        ],
    }


def _last_payload(run: BotEvalRun) -> dict:
    counts = _counts(run.checks)
    return {
        'run_id': run.id,
        'status': run.status,
        'status_label': STATUS_LABELS.get(run.status, run.status),
        'finished_at': run.finished_at.isoformat() if run.finished_at else None,
        'engine': run.engine,
        'engine_is_stub': run.engine == SHADOW_MODEL_STUB,
        'took_ms': run.took_ms,
        'passed_checks': counts['passed'],
        'failed_checks': counts['failed'],
        'not_judged': counts['not_judged'],
        'headline': _headline(run),
    }


def listing(now=None) -> dict:
    """GET bot/evals/: every script with its last finished run, and the totals."""
    now = now or timezone.now()
    finished = {
        run.scenario_id: run for run in
        BotEvalRun.objects.filter(status__in=EVAL_FINAL_STATUSES).defer('world', 'turns')
        .order_by('scenario_id', '-id').distinct('scenario_id')
    }
    running = {
        run.scenario_id: run for run in
        BotEvalRun.objects.filter(status=EVAL_RUNNING, created_at__gte=now - STALE_AFTER).defer('world', 'checks')
        .order_by('scenario_id', '-id').distinct('scenario_id')
    }
    rows, totals = [], {EVAL_PASSED: 0, EVAL_FAILED: 0, EVAL_SKIPPED: 0, EVAL_ERROR: 0}
    by_group = {group: {'id': group, 'title': title, 'total': 0, 'passed': 0, 'failed': 0} for group, title in scenarios.GROUPS}
    for scenario in scenarios.SCENARIOS:
        last = finished.get(scenario.id)
        going = running.get(scenario.id)
        if going is not None and last is not None and going.id < last.id:
            going = None
        rows.append({
            **_scenario_head(scenario),
            'last': _last_payload(last) if last is not None else None,
            'in_progress': {'run_id': going.id, 'turns_played': len(going.turns)} if going is not None else None,
        })
        group = by_group[scenario.group]
        group['total'] += 1
        if last is not None:
            totals[last.status] += 1
            if last.status in (EVAL_PASSED, EVAL_FAILED):
                group[last.status] += 1
    last_at = max((run.finished_at for run in finished.values() if run.finished_at), default=None)
    return {
        'engine': engine(),
        'summary': {
            'total': len(scenarios.SCENARIOS),
            'passed': totals[EVAL_PASSED],
            'failed': totals[EVAL_FAILED],
            'skipped': totals[EVAL_SKIPPED],
            'not_run': totals[EVAL_ERROR],
            'never_run': len(scenarios.SCENARIOS) - len([row for row in rows if row['last'] is not None]),
            'last_run_at': last_at.isoformat() if last_at else None,
        },
        'groups': list(by_group.values()),
        'scenarios': rows,
    }
