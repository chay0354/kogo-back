"""
ידע הבוט — what the new bot knows that has no home in Kogo (docs/WAHUB-CONTRACT-STAGE2.md, א).

The owner's rule (10.10.2026): a fact the system already holds — a price, a
discount, a trial fee, the registration fee, an address, an external branch,
the timetable, the capacity — is NOT written here. The bot reads it from Kogo
(shadow_tools.py) and the screen shows it under "מגיע מ-Kogo" with the fields
that are still empty. Here live the rules, the phrasings, the topics, the facts
nobody typed into a card, the contacts, the links, the nicknames, the special
days and the office hours.

Every change to an item is a version with a before/after snapshot
(KnowledgeHistory); restore copies an old `after` back. Nothing here is
written by automatic code except through a proposal the owner approved
(reviewer.approve), and the import of the old bot's texts.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.wahub import state
from apps.wahub.models import (
    KIND_ALIAS,
    KIND_BEHAVIOR_RULE,
    KIND_CONTACT,
    KIND_FACT,
    KIND_LINK,
    KIND_OFFICE_HOURS,
    KIND_PHRASING,
    KIND_PROFILE,
    KIND_SPECIAL_DAY,
    KIND_STYLE_RULE,
    KIND_TOPIC,
    KNOWLEDGE_KIND_CHOICES,
    SCOPE_BRANCH,
    SCOPE_BUSINESS,
    SCOPE_CITY,
    SCOPE_COURSE,
    SCOPE_COURSE_TYPE,
    SCOPE_LEVEL_CHOICES,
    SEND_MODE_CHOICES,
    SEND_ON_AGENT_REQUEST,
    SINGLETON_KINDS,
    SPECIAL_CLOSED,
    SPECIAL_HOURS,
    SPECIAL_OPEN,
    SPECIAL_QUIET,
    SPECIAL_STATE_CHOICES,
    WHEN_IF_ASKED,
    WHEN_INTERNAL,
    WHEN_PROACTIVE,
    WHEN_TO_SAY_CHOICES,
    KnowledgeHistory,
    KnowledgeItem,
)

KIND_LABELS = dict(KNOWLEDGE_KIND_CHOICES)
SCOPE_LABELS = dict(SCOPE_LEVEL_CHOICES)
WHEN_LABELS = dict(WHEN_TO_SAY_CHOICES)
SPECIAL_STATE_LABELS = dict(SPECIAL_STATE_CHOICES)
SEND_MODE_LABELS = dict(SEND_MODE_CHOICES)
KINDS = list(KIND_LABELS)

# What each kind keeps in `data`, flattened into its JSON. Anything else sent is dropped.
DATA_FIELDS = {
    KIND_PROFILE: ('name', 'age', 'role', 'personality', 'voice', 'address_default', 'forbidden_phrases'),
    KIND_STYLE_RULE: ('enforced_in_code',),
    KIND_BEHAVIOR_RULE: ('priority',),
    KIND_PHRASING: ('variants', 'verbatim', 'when'),
    KIND_TOPIC: ('triggers', 'steps', 'handoff_reason', 'tag'),
    KIND_FACT: ('certainty', 'question'),
    KIND_CONTACT: ('name', 'role', 'phone', 'when', 'how'),
    KIND_LINK: ('url', 'when'),
    KIND_ALIAS: ('what_customer_writes', 'means', 'means_kind'),
    KIND_SPECIAL_DAY: ('date_from', 'date_to', 'state', 'hours_from', 'hours_to', 'message'),
    KIND_OFFICE_HOURS: ('weekly', 'default_closed_message', 'send_mode'),
}
COLUMNS = (
    'kind', 'key', 'title', 'body', 'scope_level', 'scope_id', 'scope_label', 'valid_from', 'valid_until',
    'is_active', 'when_to_say', 'example_good', 'example_bad', 'source_note',
)
DAYS = ('sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat')
DAY_LABELS = dict(zip(DAYS, ('ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת')))
# Python's weekday(): Monday is 0.
_WEEKDAY_TO_KEY = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')

_TIME = re.compile(r'^([01]?\d|2[0-3]):[0-5]\d$')
_URL = re.compile(r'^https://\S+$')

STEP_KINDS = ('ask', 'say', 'handoff', 'tool', 'tag')


class KnowledgeError(ValueError):
    """A save refused, with the reason in Hebrew."""


# --- reading ------------------------------------------------------------------------

def _iso(value):
    return value.isoformat() if value else None


def item_payload(item: KnowledgeItem) -> dict:
    """The JSON of the contract: the columns, and `data` flattened beside them."""
    payload = {
        'id': item.id,
        'kind': item.kind,
        'kind_label': KIND_LABELS.get(item.kind, item.kind),
        'key': item.key,
        'title': item.title,
        'body': item.body,
        'scope': {
            'level': item.scope_level,
            'id': item.scope_id or None,
            'label': item.scope_label or SCOPE_LABELS.get(item.scope_level, ''),
        },
        'valid_from': _iso(item.valid_from),
        'valid_until': _iso(item.valid_until),
        'is_active': item.is_active,
        'when_to_say': item.when_to_say,
        'when_to_say_label': WHEN_LABELS.get(item.when_to_say, ''),
        'example_good': item.example_good,
        'example_bad': item.example_bad,
        'source_note': item.source_note,
        'updated_at': _iso(item.updated_at),
        'updated_by_name': state.user_display_name(item.updated_by) if item.updated_by_id else None,
        'version': item.version,
    }
    for name in DATA_FIELDS.get(item.kind, ()):
        payload[name] = (item.data or {}).get(name)
    if item.kind == KIND_SPECIAL_DAY:
        payload['state_label'] = SPECIAL_STATE_LABELS.get(payload.get('state') or '', '')
    if item.kind == KIND_OFFICE_HOURS:
        payload['send_mode_label'] = SEND_MODE_LABELS.get(payload.get('send_mode') or '', '')
    return payload


def item_ref(item: KnowledgeItem) -> dict:
    """The short form other objects carry: enough to show a chip and link to the item."""
    return {'id': item.id, 'kind': item.kind, 'kind_label': KIND_LABELS.get(item.kind, item.kind), 'title': item.title or item.key}


def snapshot(item: KnowledgeItem) -> dict:
    """What history keeps and restore reads: every column and the data."""
    snap = {name: getattr(item, name) for name in COLUMNS}
    snap['valid_from'] = _iso(item.valid_from)
    snap['valid_until'] = _iso(item.valid_until)
    snap['data'] = dict(item.data or {})
    return snap


def history_payload(row: KnowledgeHistory) -> dict:
    return {
        'version': row.version,
        'changed_at': _iso(row.changed_at),
        'changed_by_name': state.user_display_name(row.changed_by) if row.changed_by_id else None,
        'before': row.before,
        'after': row.after,
        'note': row.note,
    }


def listed(params=None):
    """GET knowledge/ — by kind, scope level, active flag, free search."""
    params = params or {}
    rows = KnowledgeItem.objects.select_related('updated_by')
    kind = (params.get('kind') or '').strip()
    if kind:
        rows = rows.filter(kind__in=[part for part in kind.split(',') if part in KIND_LABELS])
    level = (params.get('scope_level') or '').strip()
    if level in SCOPE_LABELS:
        rows = rows.filter(scope_level=level)
    active = (params.get('active') or '').strip()
    if active in ('1', 'true'):
        rows = rows.filter(is_active=True)
    elif active in ('0', 'false'):
        rows = rows.filter(is_active=False)
    search = ' '.join((params.get('search') or '').split())
    if search:
        rows = rows.filter(
            Q(title__icontains=search) | Q(body__icontains=search) | Q(key__icontains=search)
            | Q(example_good__icontains=search) | Q(scope_label__icontains=search) | Q(source_note__icontains=search)
        )
    return rows.order_by('kind', 'id')


# --- writing, with history ---------------------------------------------------------------

def _date(value, name):
    if value in (None, ''):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise KnowledgeError(f'תאריך לא תקין בשדה {name} (YYYY-MM-DD).')


def _clean_text(value, limit=None) -> str:
    text = str(value if value is not None else '').strip()
    return text[:limit] if limit else text


def _validate_data(kind: str, data: dict) -> dict:
    """The kind-specific fields, checked and trimmed. Refuses with a Hebrew line."""
    clean = {}
    for name in DATA_FIELDS.get(kind, ()):
        if name in data:
            clean[name] = data[name]
    if kind == KIND_LINK:
        url = _clean_text(clean.get('url'))
        if not _URL.match(url):
            raise KnowledgeError('קישור חייב להתחיל ב-https:// ולהיות ללא רווחים.')
        clean['url'] = url
    if kind == KIND_ALIAS:
        if not _clean_text(clean.get('what_customer_writes')) or not _clean_text(clean.get('means')):
            raise KnowledgeError('לכינוי נדרשים "מה הלקוח כותב" ו"למה הכוונה".')
    if kind == KIND_CONTACT:
        if not _clean_text(clean.get('name')):
            raise KnowledgeError('לאיש קשר נדרש שם.')
    if kind == KIND_SPECIAL_DAY:
        start = _date(clean.get('date_from'), 'date_from')
        if start is None:
            raise KnowledgeError('ליום מיוחד נדרש תאריך התחלה.')
        end = _date(clean.get('date_to'), 'date_to') or start
        if end < start:
            raise KnowledgeError('תאריך הסיום של היום המיוחד לפני ההתחלה.')
        clean['date_from'], clean['date_to'] = start.isoformat(), end.isoformat()
        if clean.get('state') not in SPECIAL_STATE_LABELS:
            raise KnowledgeError('מצב היום המיוחד: closed, open, hours או quiet.')
        for name in ('hours_from', 'hours_to'):
            value = _clean_text(clean.get(name))
            if value and not _TIME.match(value):
                raise KnowledgeError(f'שעה לא תקינה בשדה {name} (HH:MM).')
            clean[name] = value or None
        clean['message'] = _clean_text(clean.get('message'), 1000)
    if kind == KIND_OFFICE_HOURS:
        weekly = clean.get('weekly')
        if not isinstance(weekly, dict):
            raise KnowledgeError('שעות משרד: נדרש weekly עם שבעת הימים.')
        fixed = {}
        for day in DAYS:
            row = weekly.get(day) if isinstance(weekly.get(day), dict) else {}
            is_open = bool(row.get('open'))
            start, end = _clean_text(row.get('from')), _clean_text(row.get('to'))
            if is_open and not (_TIME.match(start) and _TIME.match(end)):
                raise KnowledgeError(f'שעות לא תקינות ליום {DAY_LABELS[day]} (HH:MM).')
            fixed[day] = {'open': is_open, 'from': start or None, 'to': end or None, 'message': _clean_text(row.get('message'), 1000)}
        clean['weekly'] = fixed
        clean['default_closed_message'] = _clean_text(clean.get('default_closed_message'), 1000)
        if clean.get('send_mode', SEND_ON_AGENT_REQUEST) not in SEND_MODE_LABELS:
            raise KnowledgeError('send_mode: on_agent_request, always או on_takeover.')
        clean.setdefault('send_mode', SEND_ON_AGENT_REQUEST)
    if kind == KIND_TOPIC:
        steps = clean.get('steps') or []
        if not isinstance(steps, list):
            raise KnowledgeError('steps של נושא הם רשימה.')
        for step in steps:
            if not isinstance(step, dict) or step.get('kind', step.get('type', 'say')) not in STEP_KINDS:
                raise KnowledgeError(f'שלב בנושא צריך kind מתוך {", ".join(STEP_KINDS)}.')
    if kind == KIND_PHRASING:
        variants = clean.get('variants')
        if variants is not None and not isinstance(variants, list):
            raise KnowledgeError('variants של נוסח הם רשימה.')
    return clean


def _apply(item: KnowledgeItem, fields: dict) -> None:
    """Put `fields` (columns and data, as the screen sends them) on the item. Refuses bad input."""
    kind = fields.get('kind', item.kind)
    if kind not in KIND_LABELS:
        raise KnowledgeError('סוג רשומה לא מוכר.')
    item.kind = kind
    if 'key' in fields:
        item.key = re.sub(r'[^\w\-]', '_', _clean_text(fields['key'], 80))
    if 'title' in fields:
        item.title = ' '.join(_clean_text(fields['title'], 200).split())
    if 'body' in fields:
        item.body = _clean_text(fields['body'], 8000)
    if 'scope_level' in fields or 'scope_id' in fields or 'scope_label' in fields:
        level = fields.get('scope_level', item.scope_level) or SCOPE_BUSINESS
        if level not in SCOPE_LABELS:
            raise KnowledgeError('היקף לא מוכר (business, city, branch, course_type, course).')
        scope_id = _clean_text(fields.get('scope_id', item.scope_id) or '', 64) if level != SCOPE_BUSINESS else ''
        label = _clean_text(fields.get('scope_label', item.scope_label) or '', 200)
        if level != SCOPE_BUSINESS and scope_id and not label:
            label = scope_label_for(level, scope_id) or ''
        item.scope_level, item.scope_id, item.scope_label = level, scope_id, (label if level != SCOPE_BUSINESS else '')
    if 'valid_from' in fields:
        item.valid_from = _date(fields['valid_from'], 'valid_from')
    if 'valid_until' in fields:
        item.valid_until = _date(fields['valid_until'], 'valid_until')
    if item.valid_from and item.valid_until and item.valid_until < item.valid_from:
        raise KnowledgeError('תאריך הסיום לפני תאריך ההתחלה.')
    if 'is_active' in fields:
        item.is_active = bool(fields['is_active'])
    if 'when_to_say' in fields:
        if fields['when_to_say'] not in WHEN_LABELS:
            raise KnowledgeError('when_to_say: proactive, if_asked או internal.')
        item.when_to_say = fields['when_to_say']
    if 'example_good' in fields:
        item.example_good = _clean_text(fields['example_good'], 2000)
    if 'example_bad' in fields:
        item.example_bad = _clean_text(fields['example_bad'], 2000)
    if 'source_note' in fields:
        item.source_note = _clean_text(fields['source_note'], 300)

    merged = dict(item.data or {})
    incoming = {name: fields[name] for name in DATA_FIELDS.get(kind, ()) if name in fields}
    if 'data' in fields and isinstance(fields['data'], dict):
        incoming = {**fields['data'], **incoming}
    merged.update(incoming)
    item.data = _validate_data(kind, merged) if (incoming or item.pk is None) else _validate_data(kind, merged)

    if kind not in (KIND_OFFICE_HOURS,) and not item.title and not item.key:
        raise KnowledgeError('נדרשת כותרת.')
    if kind == KIND_PHRASING and not item.key:
        raise KnowledgeError('לנוסח נדרש מפתח (key).')
    if kind in (KIND_PHRASING, KIND_STYLE_RULE, KIND_BEHAVIOR_RULE, KIND_FACT, KIND_TOPIC, KIND_PROFILE) and not item.body:
        raise KnowledgeError('נדרש טקסט (body).')
    if kind == KIND_PHRASING and item.is_active:
        clash = KnowledgeItem.objects.filter(kind=KIND_PHRASING, key=item.key, is_active=True).exclude(pk=item.pk)
        if clash.exists():
            raise KnowledgeError(f'כבר קיים נוסח פעיל עם המפתח "{item.key}".')
    if kind in SINGLETON_KINDS and item.is_active:
        clash = KnowledgeItem.objects.filter(kind=kind, is_active=True).exclude(pk=item.pk)
        if clash.exists():
            raise KnowledgeError(f'יש כבר רשומה פעילה מסוג {KIND_LABELS[kind]}; ערכו אותה במקום ליצור שנייה.')


@transaction.atomic
def create_item(fields: dict, user=None, note: str = '') -> KnowledgeItem:
    item = KnowledgeItem(kind=fields.get('kind', ''))
    _apply(item, fields)
    item.version = 1
    item.updated_by = user if getattr(user, 'is_authenticated', False) else None
    item.save()
    KnowledgeHistory.objects.create(
        item=item, version=1, changed_by=item.updated_by, before=None, after=snapshot(item), note=note or 'נוצר',
    )
    return item


@transaction.atomic
def update_item(item: KnowledgeItem, fields: dict, user=None, note: str = '') -> KnowledgeItem:
    """A new version — only when something actually changed."""
    item = KnowledgeItem.objects.select_for_update().get(pk=item.pk)
    before = snapshot(item)
    _apply(item, fields)
    after = snapshot(item)
    if before == after:
        return item
    item.version += 1
    item.updated_by = user if getattr(user, 'is_authenticated', False) else None
    item.save()
    KnowledgeHistory.objects.create(
        item=item, version=item.version, changed_by=item.updated_by, before=before, after=after, note=note or 'עודכן',
    )
    return item


def soft_delete(item: KnowledgeItem, user=None, note: str = 'הוסר') -> KnowledgeItem:
    return update_item(item, {'is_active': False}, user, note)


def restore(item: KnowledgeItem, version: int, user=None) -> KnowledgeItem:
    row = KnowledgeHistory.objects.filter(item=item, version=version).first()
    if row is None:
        raise KnowledgeError(f'אין גרסה {version} לרשומה הזאת.')
    fields = dict(row.after)
    fields.pop('kind', None)
    data = fields.pop('data', {}) or {}
    return update_item(item, {**fields, 'data': data}, user, note=f'שחזור לגרסה {version}')


def apply_change(change: dict, user=None, note: str = '') -> KnowledgeItem:
    """A proposal's `change` ({action, item_id, kind, after}) put on the knowledge. For reviewer.approve."""
    if not isinstance(change, dict):
        raise KnowledgeError('ההצעה אינה מכילה שינוי.')
    after = change.get('after') if isinstance(change.get('after'), dict) else {}
    action = change.get('action')
    if action == 'update':
        item = KnowledgeItem.objects.filter(pk=change.get('item_id')).first()
        if item is None:
            raise KnowledgeError('הרשומה שההצעה מעדכנת כבר לא קיימת.')
        fields = {name: value for name, value in after.items() if name != 'kind'}
        return update_item(item, fields, user, note)
    if action == 'create':
        kind = change.get('kind') or after.get('kind')
        if kind not in KIND_LABELS:
            raise KnowledgeError('ההצעה לא אומרת איזה סוג רשומה ליצור.')
        fields = {**after, 'kind': kind}
        if kind == KIND_PHRASING and not fields.get('key'):
            fields['key'] = f'proposal_{int(timezone.now().timestamp())}'
        return create_item(fields, user, note)
    raise KnowledgeError('פעולה לא מוכרת בהצעה (create או update).')


# --- scope labels from Kogo ----------------------------------------------------------------

def scope_label_for(level: str, scope_id: str) -> str | None:
    try:
        if level == SCOPE_BRANCH:
            from apps.core.models import Branch
            return Branch.objects.filter(pk=scope_id).values_list('name', flat=True).first()
        if level == SCOPE_CITY:
            from apps.core.models import City
            return City.objects.filter(pk=scope_id).values_list('name', flat=True).first()
        if level == SCOPE_COURSE_TYPE:
            from apps.courses.models import CourseType
            return CourseType.objects.filter(pk=scope_id).values_list('name', flat=True).first()
        if level == SCOPE_COURSE:
            from apps.courses.models import Course
            return Course.objects.filter(pk=scope_id).values_list('name', flat=True).first()
    except Exception:  # a malformed id is not an error worth a 500
        return None
    return None


# --- what the bot reads from Kogo (read only) ----------------------------------------------

def from_kogo() -> dict:
    """GET knowledge/from-kogo/: the Kogo data the bot answers from, with the holes marked."""
    from apps.core.models import Branch
    from apps.courses.models import Course, CourseType
    from apps.customers.financial_models import Discount
    from apps.enrollments.models import TrialBlockedDate
    from apps.enrollments.trial_reminders import configured_blocked_trial_lesson_dates

    branches = []
    for branch in Branch.objects.filter(is_active=True).select_related('city').order_by('name'):
        missing = []
        if not branch.is_external:
            for name, value in (('address', branch.address), ('phone', branch.phone), ('directions', branch.arrival_directions)):
                if not (value or '').strip():
                    missing.append(name)
        elif not (branch.external_link or '').strip():
            missing.append('external_link')
        branches.append({
            'id': str(branch.id),
            'name': branch.name,
            'city': branch.city.name if branch.city_id else '',
            'is_external': branch.is_external,
            'external_link': branch.external_link or '',
            'address': branch.address or '',
            'phone': branch.phone or '',
            'manager_name': branch.manager_name or '',
            'directions': branch.arrival_directions or '',
            'missing': missing,
            'edit_path': '/branches',
        })

    course_types = [{
        'id': str(kind.id),
        'name': kind.name,
        'description': kind.description or '',
        'trial_bring_note': kind.trial_bring_note or '',
        'missing': [] if (kind.trial_bring_note or '').strip() else ['trial_bring_note'],
        'edit_path': '/courses',
    } for kind in CourseType.objects.filter(is_active=True).order_by('name')]

    courses = list(
        Course.objects.filter(is_active=True).select_related('branch', 'course_type').order_by('branch__name', 'name')
    )
    pricing = []
    for course in courses:
        missing = []
        if course.price is None or course.price <= 0:
            missing.append('price')
        if course.trial_lesson_is_paid and not course.trial_lesson_price:
            missing.append('trial_lesson_price')
        pricing.append({
            'id': str(course.id),
            'name': course.name,
            'display_id': course.display_id,
            'branch': course.branch.name,
            'branch_id': str(course.branch_id),
            'course_type': course.course_type.name if course.course_type_id else '',
            'price': str(course.price) if course.price is not None else None,
            'trial_is_paid': course.trial_lesson_is_paid,
            'trial_price': str(course.trial_lesson_price) if course.trial_lesson_price is not None else None,
            'registration_fee_override': str(course.registration_fee_override) if course.registration_fee_override is not None else None,
            'min_age': course.min_age,
            'max_age': course.max_age,
            'show_in_widget': course.show_in_widget,
            'external_link': course.external_link or '',
            'missing': missing,
            'edit_path': '/courses',
        })

    discounts = [{
        'id': str(row.id),
        'name': row.name,
        'type': row.discount_type,
        'type_label': dict(Discount.DISCOUNT_TYPE_CHOICES).get(row.discount_type, row.discount_type),
        'value': str(row.value),
        'start_date': _iso(row.start_date),
        'end_date': _iso(row.end_date),
        'is_built_in': row.is_built_in,
        'is_active': row.is_active,
        # A built-in discount at 0 is one nobody has configured (discount_service.py).
        'configured': not (row.is_built_in and row.value == 0),
        'edit_path': '/customers',
    } for row in Discount.objects.all().order_by('name')]

    today = state.now_israel_date()
    blocked = {d: '' for d in configured_blocked_trial_lesson_dates()}
    for row in TrialBlockedDate.objects.prefetch_related('lessons'):
        blocked[row.date] = row.reason or ''
    blocked_dates = [
        {'date': d.isoformat(), 'reason': reason, 'is_past': d < today}
        for d, reason in sorted(blocked.items())
    ]

    return {
        'branches': branches,
        'course_types': course_types,
        'pricing_summary': {
            'courses': pricing,
            'courses_total': len(pricing),
            'courses_without_price': sum(1 for row in pricing if 'price' in row['missing']),
            'paid_trials': sum(1 for row in pricing if row['trial_is_paid']),
        },
        'registration_fee': getattr(settings, 'REGISTRATION_FEE_ILS', None),
        'discounts': discounts,
        'blocked_dates': blocked_dates,
        'note': 'קריאה בלבד. עורכים בכרטיס הסניף, החוג, התחום או ההנחה; הבוט קורא משם.',
    }


# --- office hours and special days -----------------------------------------------------------

def office_hours_item() -> KnowledgeItem | None:
    return KnowledgeItem.objects.filter(kind=KIND_OFFICE_HOURS, is_active=True).order_by('id').first()


def _valid_on(item: KnowledgeItem, day: date) -> bool:
    if item.valid_from and day < item.valid_from:
        return False
    if item.valid_until and day > item.valid_until:
        return False
    return True


def special_days(day_from: date, day_to: date, *, scope_level: str | None = None, scope_ids=None):
    """Active special days that touch [day_from, day_to]; the business's, plus the given scopes."""
    rows = []
    for item in KnowledgeItem.objects.filter(kind=KIND_SPECIAL_DAY, is_active=True).order_by('id'):
        data = item.data or {}
        try:
            start = date.fromisoformat(data.get('date_from') or '')
            end = date.fromisoformat(data.get('date_to') or data.get('date_from') or '')
        except ValueError:
            continue
        if end < day_from or start > day_to:
            continue
        if item.scope_level != SCOPE_BUSINESS:
            if scope_level is None or item.scope_level != scope_level or item.scope_id not in (scope_ids or ()):
                continue
        rows.append(item)
    return rows


def _parse_time(value) -> time | None:
    if not value or not _TIME.match(str(value)):
        return None
    hours, minutes = str(value).split(':')
    return time(int(hours), int(minutes))


def office_hours_now(now=None) -> dict:
    """
    GET knowledge/office-hours/now/ — by Israel's clock. A special day of the
    whole business overrides the weekly line: closed shuts the office, `hours`
    replaces the times, `open` keeps the weekly line, `quiet` keeps the weekly
    line but the day's message is what goes out when closed.
    """
    now = timezone.localtime(now or timezone.now())
    today = now.date()
    item = office_hours_item()
    weekly = (item.data or {}).get('weekly') if item else None
    default_message = (item.data or {}).get('default_closed_message', '') if item else ''
    send_mode = (item.data or {}).get('send_mode', SEND_ON_AGENT_REQUEST) if item else SEND_ON_AGENT_REQUEST
    key = _WEEKDAY_TO_KEY[now.weekday()]
    line = dict((weekly or {}).get(key) or {}) if weekly else {'open': False, 'from': None, 'to': None, 'message': ''}
    today_row = {
        'day': key, 'day_label': DAY_LABELS[key],
        'open': bool(line.get('open')), 'from': line.get('from'), 'to': line.get('to'), 'message': line.get('message') or '',
    }

    special = next((row for row in special_days(today, today) if row.scope_level == SCOPE_BUSINESS), None)
    is_open = False
    closed_message = ''
    if item is None:
        is_open, closed_message = False, 'שעות המשרד עוד לא הוגדרו'
    else:
        start, end = _parse_time(line.get('from')), _parse_time(line.get('to'))
        if special is not None:
            data = special.data or {}
            if data.get('state') == SPECIAL_CLOSED:
                start = end = None
                today_row['open'] = False
            elif data.get('state') == SPECIAL_HOURS:
                start = _parse_time(data.get('hours_from')) or start
                end = _parse_time(data.get('hours_to')) or end
                today_row.update(open=True, **{'from': start.strftime('%H:%M') if start else None, 'to': end.strftime('%H:%M') if end else None})
        if today_row['open'] and start and end:
            is_open = start <= now.time() < end
        if not is_open:
            closed_message = (
                ((special.data or {}).get('message') if special is not None else '')
                or today_row['message'] or default_message or 'המשרד סגור כרגע'
            )
    return {
        'now': now.isoformat(),
        'open': is_open,
        'today': today_row,
        'special': item_payload(special) if special is not None else None,
        'message_if_closed': closed_message or None,
        'send_mode': send_mode,
        'send_mode_label': SEND_MODE_LABELS.get(send_mode, send_mode),
        'configured': item is not None,
    }


# --- what goes into the shadow's context ---------------------------------------------------------

CONTEXT_KINDS = (
    KIND_PROFILE, KIND_STYLE_RULE, KIND_BEHAVIOR_RULE, KIND_PHRASING, KIND_TOPIC, KIND_FACT,
    KIND_CONTACT, KIND_LINK, KIND_ALIAS,
)
UPCOMING_SPECIAL_DAYS = timedelta(days=45)


def relevant_items(*, today: date, branch_id=None, city_id=None, course_type_id=None, course_id=None) -> list:
    """
    The active, in-date items for a conversation: everything of the business,
    plus what is scoped to the branch / city / type / course the conversation
    is about, plus the special days of today and the coming weeks.
    """
    wanted = {
        SCOPE_BRANCH: str(branch_id) if branch_id else None,
        SCOPE_CITY: str(city_id) if city_id else None,
        SCOPE_COURSE_TYPE: str(course_type_id) if course_type_id else None,
        SCOPE_COURSE: str(course_id) if course_id else None,
    }
    rows = []
    for item in KnowledgeItem.objects.filter(kind__in=CONTEXT_KINDS, is_active=True).order_by('kind', 'id'):
        if not _valid_on(item, today):
            continue
        if item.scope_level != SCOPE_BUSINESS and wanted.get(item.scope_level) != item.scope_id:
            # A scoped item whose scope is not yet known in the conversation is
            # still worth telling the bot about — it decides by the scope label.
            if wanted.get(item.scope_level) is not None:
                continue
        rows.append(item)
    scoped = {level: [value] for level, value in wanted.items() if value}
    for level, ids in scoped.items():
        rows += special_days(today, today + UPCOMING_SPECIAL_DAYS, scope_level=level, scope_ids=ids)
    rows += [row for row in special_days(today, today + UPCOMING_SPECIAL_DAYS) if row.scope_level == SCOPE_BUSINESS]
    seen, unique = set(), []
    for item in rows:
        if item.id not in seen:
            seen.add(item.id)
            unique.append(item)
    return unique


def phrasings_by_key(items) -> dict:
    return {item.key: item for item in items if item.kind == KIND_PHRASING and item.key}


def render_phrasing(item: KnowledgeItem, **variables) -> str:
    """A phrasing with its {משתנים} filled; a variable nobody supplied stays as it is."""
    text = item.body
    for name, value in variables.items():
        if value is None:
            continue
        text = text.replace('{' + name + '}', str(value))
    return text


def describe_for_prompt(items, hours: dict) -> str:
    """The knowledge as the system prompt reads it, grouped by kind, each item numbered by id."""
    by_kind: dict = {}
    for item in items:
        by_kind.setdefault(item.kind, []).append(item)
    parts = []

    profile = by_kind.get(KIND_PROFILE)
    if profile:
        parts.append('## מי את\n' + profile[0].body)

    def lines(kind: str, heading: str, fmt):
        rows = by_kind.get(kind)
        if rows:
            parts.append(f'## {heading}\n' + '\n'.join(fmt(item) for item in rows))

    def scope(item):
        return f' [היקף: {item.scope_label}]' if item.scope_level != SCOPE_BUSINESS and item.scope_label else ''

    def when(item):
        return {WHEN_IF_ASKED: ' (רק אם שואלים)', WHEN_INTERNAL: ' (הנחיה פנימית, לא לומר ללקוח)'}.get(item.when_to_say, '')

    def examples(item):
        extra = ''
        if item.example_good:
            extra += f'\n   ✓ נכון: {item.example_good}'
        if item.example_bad:
            extra += f'\n   ✗ שגוי: {item.example_bad}'
        return extra

    lines(KIND_STYLE_RULE, 'כללי עיצוב (חובה בכל הודעה)', lambda i: f'- [#{i.id}] {i.title}: {i.body}{examples(i)}')
    lines(KIND_BEHAVIOR_RULE, 'כללי התנהגות', lambda i: f'- [#{i.id}] {i.title}{scope(i)}: {i.body}{examples(i)}')
    lines(KIND_FACT, 'עובדות (רק מה שכתוב כאן; הכול אחר מהכלים)', lambda i: f'- [#{i.id}] {i.title}{scope(i)}{when(i)}: {i.body}')
    lines(KIND_PHRASING, 'נוסחים (מילה במילה כשמסומן; {משתנה} ממלאים)', lambda i: f'- [#{i.id}] {i.key}' + (' (מילה במילה)' if (i.data or {}).get('verbatim') else '') + (f' — מתי: {(i.data or {}).get("when")}' if (i.data or {}).get('when') else '') + f':\n"""{i.body}"""')

    def topic(item):
        steps = (item.data or {}).get('steps') or []
        text = f'- [#{item.id}] {item.title}{scope(item)}: {item.body}'
        trig = (item.data or {}).get('triggers')
        if trig:
            text += f'\n   מזהים לפי: {", ".join(trig) if isinstance(trig, list) else trig}'
        for index, step in enumerate(steps, 1):
            kind = step.get('kind', step.get('type', 'say'))
            cond = f' (אם {step["condition"]})' if step.get('condition') else ''
            text += f'\n   {index}. {kind}{cond}: {step.get("text", "")}'
        if (item.data or {}).get('handoff_reason'):
            text += f'\n   העברה לנציג: {(item.data or {}).get("handoff_reason")}'
        if (item.data or {}).get('tag'):
            text += f'\n   בסיום לתייג: {(item.data or {}).get("tag")}'
        return text

    lines(KIND_TOPIC, 'נושאים (תסריטים)', topic)
    lines(KIND_CONTACT, 'אנשי קשר והפניות', lambda i: f'- [#{i.id}] {(i.data or {}).get("name", i.title)} — {(i.data or {}).get("role", "")}' + (f' · {(i.data or {}).get("phone")}' if (i.data or {}).get('phone') else '') + f'. מתי: {(i.data or {}).get("when", "")}. איך: {(i.data or {}).get("how", "")}')
    lines(KIND_LINK, 'קישורים (תמיד עם https:// מלא)', lambda i: f'- [#{i.id}] {i.key or i.title}: {(i.data or {}).get("url")} — מתי: {(i.data or {}).get("when", i.body)}')
    lines(KIND_ALIAS, 'כינויים (מה הלקוח כותב → למה הכוונה)', lambda i: f'- [#{i.id}] "{(i.data or {}).get("what_customer_writes")}" → {(i.data or {}).get("means")}')
    lines(KIND_SPECIAL_DAY, 'ימים מיוחדים קרובים (להציג רק עתידיים, בפורמט 🔹 *תאריך* שם - מצב)', lambda i: f'- [#{i.id}] {(i.data or {}).get("date_from")}' + (f'–{(i.data or {}).get("date_to")}' if (i.data or {}).get('date_to') and (i.data or {}).get('date_to') != (i.data or {}).get('date_from') else '') + f' {i.title}{scope(i)}: {SPECIAL_STATE_LABELS.get((i.data or {}).get("state", ""), "")}' + (f' {(i.data or {}).get("hours_from") or ""}-{(i.data or {}).get("hours_to") or ""}'.rstrip('-') if (i.data or {}).get('state') in (SPECIAL_HOURS, SPECIAL_QUIET) and ((i.data or {}).get('hours_from') or (i.data or {}).get('hours_to')) else '') + (f' — "{(i.data or {}).get("message")}"' if (i.data or {}).get('message') else ''))

    today = hours.get('today') or {}
    status = 'פתוח עכשיו' if hours.get('open') else 'סגור עכשיו'
    hours_line = f'## שעות המשרד\nעכשיו: {hours.get("now", "")} — המשרד {status}.'
    if today.get('open'):
        hours_line += f' היום ({today.get("day_label")}) פתוח {today.get("from")}–{today.get("to")}.'
    else:
        hours_line += f' היום ({today.get("day_label")}) המשרד סגור.'
    if hours.get('message_if_closed'):
        hours_line += f'\nהודעת "סגור" (לשלוח רק כשמבקשים נציג מחוץ לשעות, לפי מצב השליחה "{hours.get("send_mode_label")}"):\n"""{hours["message_if_closed"]}"""'
    parts.append(hours_line)
    return '\n\n'.join(parts)
