"""Where each of the old software's locations goes in kogo: business, category, branch.

The old software's "מיקום" mixes two different things: the branches the
lessons are taught in ("כפר סבא", "סניף מתנ"ס עמישב פ"ת") and the accountant's
numbered sections ("22 מרצנדייס", "01 הוצאות חברה כללי"). kogo's branches have
their own names, which are not the old software's, so nothing here is final:
it suggests, the owner confirms or changes each one in the preview, and the
commit writes only what the owner confirmed.

A suggestion is resolved against what exists in the database at preview time,
never against a hard-coded list — a branch renamed or added in kogo is found
the next time a file is previewed.

Branch locations belong under the category סניפים when such a category exists:
that is what the category means everywhere else in kogo (the documents wizard
asks for a branch only under it — NewDocumentDialog's branchFieldApplies).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from apps.legacy_import.parser import _QUOTES

BRANCHES_CATEGORY = 'סניפים'
GENERAL_CATEGORY = 'כללי'
SHOWS_BUSINESS = 'הצגות חיצוניות'
BRAND_BUSINESS = 'מותג קוגומלו'

# Words that say what kind of place it is, not which one. "מרכז זמיר" and
# "מתנ"ס זמיר" are the same place.
GENERIC_PLACE_WORDS = frozenset({'סניף', 'מתנס', 'קאנטרי', 'מרכז', 'מרכזים', 'סניפים'})

# Abbreviations the office types, spelled out so they meet the branch names.
ABBREVIATIONS = (
    ('פ"ת', 'פתח תקווה'),
    ('ת"א', 'תל אביב'),
    ('ר"ג', 'רמת גן'),
    ('ראשל"צ', 'ראשון לציון'),
    ('כ"ס', 'כפר סבא'),
    ('פת', 'פתח תקווה'),
    ('תקוה', 'תקווה'),
)

_SECTION = re.compile(r'^\s*\d{1,3}\s')
_PUNCTUATION = re.compile(r'[^\w\s]')

KIND_BRANCH = 'branch'
KIND_SECTION = 'section'
KIND_EXPENSES = 'expenses'
KIND_UNKNOWN = 'unknown'

# A place named in the documents' פרטים counts once it is on this many of them,
# and on at least twice as many as the next-best branch.
DETAILS_MIN_DOCUMENTS = 3


@dataclass(frozen=True)
class Option:
    id: str
    name: str
    business_id: str = ''  # categories only
    is_active: bool = True


def place_tokens(text: str) -> list:
    """'סניף מתנ"ס עמישב פ"ת' -> ['עמישב', 'פתח', 'תקווה']."""
    text = (text or '').translate(_QUOTES)
    for short, full in ABBREVIATIONS:
        text = re.sub(rf'(?<!\w){re.escape(short)}(?!\w)', full, text)
    text = text.replace('"', '').replace("'", '')
    text = _PUNCTUATION.sub(' ', text)
    return [token for token in text.split() if not token.isdigit() and token not in GENERIC_PLACE_WORDS]


def _same_word(a: str, b: str) -> bool:
    # "הספורטן" and "ספורטן": the definite article is not part of the name.
    if a == b:
        return True
    strip = lambda word: word[1:] if len(word) > 3 and word.startswith('ה') else word  # noqa: E731
    return strip(a) == strip(b)


def _overlap(a: list, b: list) -> int:
    return sum(1 for word in b if any(_same_word(word, other) for other in a))


def match_branch_by_name(location: str, branches: list):
    """
    The branch whose name is inside the location, or the location inside the
    branch's name — every distinctive word of one found in the other. Two
    branches that fit equally well is no answer: the owner picks.
    """
    loc = place_tokens(location)
    if not loc:
        return None
    scored = []
    for branch in branches:
        words = place_tokens(branch.name)
        if not words:
            continue
        common = _overlap(loc, words)
        if common and (common == len(words) or common == len(loc)):
            scored.append(((common, -abs(len(words) - len(loc)), branch.is_active), branch))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def match_branch_by_details(details: list, branches: list):
    """
    The branch the documents' פרטים name, when the location itself does not.

    "סניף כפר גנים ג פ"ת" is where מרכז זמיר is: its documents say "הדרכת
    קפוארה מתנס זמיר כפג". A branch counts when every distinctive word of its
    name is on the same document.
    """
    if not details:
        return None
    texts = [set(place_tokens(text)) for text in details]
    counts = []
    for branch in branches:
        words = place_tokens(branch.name)
        if not words:
            continue
        hits = sum(1 for text in texts if all(any(_same_word(word, t) for t in text) for word in words))
        if hits:
            counts.append((hits, branch))
    if not counts:
        return None
    counts.sort(key=lambda item: item[0], reverse=True)
    best, branch = counts[0]
    runner_up = counts[1][0] if len(counts) > 1 else 0
    if best >= DETAILS_MIN_DOCUMENTS and best >= 2 * runner_up:
        return branch
    return None


def _first(options, predicate):
    return next((option for option in options if option.is_active and predicate(option)), None)


def _general_category(business, categories):
    # Only כללי: a business's one other category is a specific line of it
    # (מותג קוגומלו's is the merch shipments), and the brand's general
    # income is not merch.
    if business is None:
        return None
    return _first(categories, lambda c: c.business_id == business.id and c.name.strip() == GENERAL_CATEGORY)


def suggest(location: str, details: list, businesses: list, categories: list, branches: list) -> dict:
    """
    {kind, business_id, category_id, branch_id, reason} for one location.

    kind: 'branch' (a place lessons are taught), 'section' (an accounting
    section), 'expenses' (an expense reimbursement — left for the owner, and
    flagged, because it is not income of any business), or 'unknown'.
    """
    plain = (location or '').translate(_QUOTES)
    # A closed branch is history; the owner's rule is that the future wins.
    branches = [branch for branch in branches if branch.is_active]
    by_id = {b.id: b for b in businesses}
    result = {'kind': KIND_UNKNOWN, 'business_id': None, 'category_id': None, 'branch_id': None,
              'reason': 'לא נמצאה התאמה — בחרו ידנית', 'flag': False}

    def section(business=None, category=None, reason=''):
        if category is not None and business is None:
            business = by_id.get(category.business_id)
        result.update(
            kind=KIND_SECTION,
            business_id=business.id if business else None,
            category_id=category.id if category else None,
            reason=reason if business else f'{reason} — לא נמצא בעסקים, בחרו ידנית',
        )
        return result

    if 'החזר הוצאות' in plain:
        result.update(kind=KIND_EXPENSES, flag=True, reason='החזר הוצאות — אינו הכנסה; נשאר ללא שיוך')
        return result
    if 'מרצנדייס' in plain:
        category = _first(categories, lambda c: 'מרצנדייס' in c.name)
        return section(category=category, reason='מילת מפתח: מרצנדייס')
    if 'הצגות' in plain:
        business = _first(businesses, lambda b: b.name.strip() == SHOWS_BUSINESS) or \
            _first(businesses, lambda b: 'הצגות' in b.name)
        return section(business, _general_category(business, categories), 'מילת מפתח: הצגות')
    if 'מותג' in plain:
        business = _first(businesses, lambda b: b.name.strip() == BRAND_BUSINESS) or \
            _first(businesses, lambda b: 'קוגומלו' in b.name)
        return section(business, _general_category(business, categories), 'מילת מפתח: מותג')
    if 'הוצאות' in plain:
        result.update(kind=KIND_EXPENSES, flag=True, reason='סעיף הוצאות — אינו הכנסה; נשאר ללא שיוך')
        return result
    if _SECTION.match(plain):
        result.update(kind=KIND_SECTION, reason='סעיף הנהלת חשבונות — בחרו ידנית')
        return result

    branch = match_branch_by_name(location, branches)
    reason = 'לפי שם הסניף'
    if branch is None:
        branch = match_branch_by_details(details, branches)
        reason = 'לפי המקום שבפרטי המסמכים'
    if branch is None:
        result.update(kind=KIND_BRANCH if place_tokens(location) else KIND_UNKNOWN)
        return result
    category = _first(categories, lambda c: c.name.strip() == BRANCHES_CATEGORY)
    result.update(
        kind=KIND_BRANCH,
        branch_id=branch.id,
        category_id=category.id if category else None,
        business_id=category.business_id if category else None,
        reason=reason,
    )
    return result


def load_options():
    """(businesses, categories, branches) as Options, in the order kogo lists them."""
    from apps.core.models import Branch, Business, BusinessCategory

    businesses = [Option(str(b.id), b.name, is_active=b.is_active) for b in Business.objects.all()]
    categories = [
        Option(str(c.id), c.name, business_id=str(c.business_id), is_active=c.is_active)
        for c in BusinessCategory.objects.select_related('business').all()
    ]
    branches = [Option(str(b.id), b.name, is_active=b.is_active) for b in Branch.objects.all()]
    return businesses, categories, branches


def options_payload(businesses, categories, branches) -> dict:
    return {
        'businesses': [{'id': b.id, 'name': b.name, 'is_active': b.is_active} for b in businesses],
        'categories': [
            {'id': c.id, 'name': c.name, 'business_id': c.business_id, 'is_active': c.is_active}
            for c in categories
        ],
        'branches': [{'id': b.id, 'name': b.name, 'is_active': b.is_active} for b in branches],
    }
