"""
"אין כרגע מה להציע" — looked at every day (docs/WAHUB-CONTRACT-LEADS-NO-ONE-LOST.md, ד).

A lead in `nothing_to_offer` — no branch near them, no class that fits, the
class is full — was "kept: we will tell them when something opens", and
nothing ever looked whether something did. Now, once a day, every such lead
whose field, age or grade, and city or branch are known is held against the
classes as they are today, by the bot's own search (shadow_tools.find_courses
— the one that answers customers; it only reads). A class that fits them and
has a free seat is kept on the contact (`lead_opened`), and the stage moves the
lead to the office: "נפתח: … — להציע לו" (lead_stage.opening_found). Nothing is
sent to the customer: what to tell them is the office's to decide.

    checked_on   the day the lead was last looked at — one look a day, whatever the cron does
    found_at     the moment what fits was found
    what         "נפתח: קפוארה גן חובה בראש העין" — the classes, by branch

The same classes found again do not move `found_at`: once the office has dealt
with them (a mark, or the customer wrote) the lead does not come back to the
office tomorrow for the same classes. Other classes are news, and do. A day
with nothing found changes nothing but `checked_on`.

A lead whose field, age or place is not known is not searched for (the screen
says what is missing — leads.no_step_because); it is marked as looked at all
the same, so that it does not stand in line before the others on every call.
"""
from __future__ import annotations

import logging
import re
import time

from django.db.models import F, Q
from django.db.models.fields.json import KeyTextTransform
from django.utils import timezone

from apps.courses import age_groups
from apps.wahub import lead_stage, state
from apps.wahub.models import EVENT_LEAD_STAGE, KIND_ALIAS, LEAD_NOTHING_TO_OFFER, Contact, KnowledgeItem

logger = logging.getLogger(__name__)

# One cron call: how long the look may take at most, and how many leads it reads.
CHECK_SECONDS = 2.0
BATCH = 50
# "נפתח: …" is the start of the office's next step (200 characters with " — להציע לו").
WHAT_CHARS = 150
NAMES_PER_BRANCH = 3

_NUMBER = re.compile(r'^\s*(\d+(?:\.\d+)?)')


def _age(child: dict, written: str) -> float | None:
    """The child's age in years: what the summary kept, else the age on the card when it is a number."""
    years, months = child.get('age_years'), child.get('age_months')
    if isinstance(years, int) and not isinstance(years, bool):
        extra = months if isinstance(months, int) and not isinstance(months, bool) else 0
        return years + extra / 12
    found = _NUMBER.match(written or '')
    return float(found.group(1)) if found else None


def _grade(child: dict, written: str) -> str:
    """
    The class or kindergarten as it was said ("כיתה ב", "גן חובה") — only one the
    age scale knows. A bare number on the card is an age, never a grade ("5" is
    five years old, not the fifth grade).
    """
    for text in (child.get('grade'), '' if _NUMBER.match(written or '') else written):
        text = str(text or '').strip()
        if text and age_groups.codes_for_grade(text):
            return text
    return ''


def search_terms(contact: Contact) -> dict | None:
    """
    What to look for, as find_courses takes it — {course_type, age, grade, city,
    branch} — from what the conversation said and the card keeps; None when the
    field, the age or grade, or the place is not known: then nothing is looked for.
    """
    kept = contact.lead_ai if isinstance(contact.lead_ai, dict) else {}
    child = kept.get('child') if isinstance(kept.get('child'), dict) else {}
    course_type = (contact.lead_course_type or kept.get('course_type') or contact.known_course_type or '').strip()
    age = _age(child, contact.known_child_age)
    grade = _grade(child, contact.known_child_age)
    branch = (kept.get('branch_name') or contact.known_branch_name or '').strip()
    city = (kept.get('city') or contact.known_city or '').strip()
    if not course_type or (age is None and not grade) or not (branch or city):
        return None
    return {'course_type': course_type, 'age': age, 'grade': grade, 'city': city, 'branch': branch}


def what_is_open(terms: dict, *, now=None, items=()) -> str:
    """
    "נפתח: …" — the classes the search finds for these terms today that have a
    seat free in one of their lessons; '' when there is none. Every class the
    branches hold for the field and the age is asked for (`show_all`): with the
    place and the age known, that leaves out only the questions the bot asks a
    customer (which branch of a city, which grade on the line between two groups),
    which would hide a class that did open. A class whose every lesson is full is
    not open — that is what kept a lead whose class is full here.
    """
    from apps.wahub import shadow_tools

    result = shadow_tools.find_courses(
        shadow_tools.ToolContext(now=now, items=items), city=terms['city'], branch=terms['branch'],
        course_type=terms['course_type'], age=terms['age'], grade=terms['grade'], show_all=True,
    )
    if result.get('need') or not result.get('courses'):
        return ''
    by_branch: dict = {}
    for course in result['courses']:
        if any(not lesson.get('is_full') for lesson in course.get('lessons') or []):
            by_branch.setdefault(course.get('branch') or '', []).append(course['name'])
    if not by_branch:
        return ''
    parts = [
        ', '.join(list(dict.fromkeys(names))[:NAMES_PER_BRANCH]) + (f' ב{branch}' if branch else '')
        for branch, names in by_branch.items()
    ]
    return ('נפתח: ' + '; '.join(parts))[:WHAT_CHARS]


def check(*, now=None, deadline: float | None = None, batch: int = BATCH) -> dict:
    """
    The cron's slice: the leads with nothing to offer that were not looked at
    today, the longest unlooked first, each finished and saved on its own — a
    call that runs out of time (`deadline`, time.monotonic) loses nothing, and
    the next one carries on. A lead the office already has an opening for is
    not searched again until that opening is done with. Reads the classes;
    writes `lead_opened` and, when something opened, the stage. Sends nothing.
    Returns {openings_checked, openings_found}.
    """
    now = now or timezone.now()
    today = timezone.localtime(now).date().isoformat()
    counts = {'openings_checked': 0, 'openings_found': 0}
    rows = (
        Contact.objects.filter(lead_stage=LEAD_NOTHING_TO_OFFER)
        .alias(_checked_on=KeyTextTransform('checked_on', 'lead_opened'))
        .filter(Q(_checked_on__isnull=True) | Q(_checked_on__lt=today))
        .order_by(F('_checked_on').asc(nulls_first=True), 'id')[:batch]
    )
    items = None
    for contact in rows:
        if deadline is not None and time.monotonic() >= deadline:
            break
        kept = dict(contact.lead_opened) if isinstance(contact.lead_opened, dict) else {}
        terms = search_terms(contact)
        found = ''
        if terms is not None and lead_stage.opening_found(contact) is None:
            if items is None:
                # The nicknames of branches and classes the office filed, read once for the whole call.
                items = list(KnowledgeItem.objects.filter(kind=KIND_ALIAS, is_active=True))
            try:
                found = what_is_open(terms, now=now, items=items)
            except Exception:
                # Looked at again tomorrow; a lead that cannot be searched must not hold up the others.
                logger.exception('wahub lead openings: the classes for contact %s could not be looked at', contact.pk)
            counts['openings_checked'] += 1
        kept['checked_on'] = today
        if found and found != kept.get('what'):
            kept.update(found_at=now.isoformat(), what=found)
            state.touch(contact.pk, lead_opened=kept)
            state.log_event(contact.pk, EVENT_LEAD_STAGE, f'הבדיקה היומית: {found}')
            # The stage at once — the lead is the office's now; a fault there is the scan's to put right.
            lead_stage.refresh_quietly(contact.pk)
            counts['openings_found'] += 1
        else:
            # Only the day of the look: nothing the screen shows has changed.
            Contact.objects.filter(pk=contact.pk).update(lead_opened=kept)
    return counts
