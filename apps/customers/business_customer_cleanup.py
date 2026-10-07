"""Finding business customers that are one customer, and making them one card.

Two things put a customer in the list twice:

* One customer under a card per branch — "עופר ומאור בית ספר למשחק סניף מינץ"
  and "… סניף ראש העין". The branch is not part of who they are; one card, named
  without it.
* A customer who changed name — the previous software opened a new customer
  for the new name. One card, under the newest name.

suggest_groups() only looks: cards that share a company number or ID, cards
whose names are the same once the branch is taken off, and cards that share a
phone. Nothing is merged by a rule — a network's community centres share a
company number and are different customers, and two parents can share a phone
— so the office decides each group: merge(), or keep_apart().

merge() moves everything that points at the cards going away to the one that
stays — documents, payment links, signatures, tenancies, standing orders, the
previous software's history, location changes — fills the blanks the survivor
has from what the others knew, records the whole of each card that goes away
(BusinessCustomerCleanup), and only then deletes them.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime

from django.db import transaction
from django.db.models import Count, Max

from apps.customers.cleanup_models import BusinessCustomerCleanup

REASON_SAME_NAME = 'same_name'
REASON_SAME_NUMBER = 'same_number'
REASON_SAME_PHONE = 'same_phone'
REASON_LABELS = {
    REASON_SAME_NAME: 'אותו שם, בלי הסניף',
    REASON_SAME_NUMBER: 'אותו ח"פ / ת"ז',
    REASON_SAME_PHONE: 'אותו טלפון',
}
# The order they are shown in: the surest first.
REASON_ORDER = (REASON_SAME_NAME, REASON_SAME_NUMBER, REASON_SAME_PHONE)

# One number under two names is a change of name when the older name stopped
# being used long before the newer one's last document; two names active side
# by side are more likely two customers (a network's community centres).
RENAME_GAP_DAYS = 180

_QUOTES = str.maketrans({'״': '"', '”': '"', '“': '"', '׳': "'", '’': "'", '‘': "'", '`': "'"})
_SPACES = re.compile(r'\s+')
# "… סניף מינץ", "… - סניף ראש העין", "… (סניף כפר סבא)": the branch, to the end of the name.
_BRANCH_PART = re.compile(r'\s*[-–,(]?\s*סניף\s+[^)]*\)?\s*$')
_PARENTHESES = re.compile(r'\s*\([^)]*\)\s*')
# 'יובא מהתוכנה הקודמת: … אחרון חשבונית מס 40413 מ-15/09/2026'
_NOTE_DATE = re.compile(r'מ-(\d{2})/(\d{2})/(\d{4})')


class CleanupError(ValueError):
    """The merge asked for cannot be done. The message is the office's, in Hebrew."""


def without_branch(name: str) -> str:
    """The name as it is written, with a trailing 'סניף …' taken off."""
    plain = _SPACES.sub(' ', (name or '').translate(_QUOTES)).strip()
    stripped = _BRANCH_PART.sub('', plain).strip(' -–,')
    return stripped or plain


def name_key(name: str) -> str:
    """What two names are compared by: no branch, no bracketed remark, spacing and case aside."""
    plain = _PARENTHESES.sub(' ', without_branch(name))
    return _SPACES.sub(' ', plain).strip(' -–,').casefold()


def _digits(value) -> str:
    return re.sub(r'\D', '', str(value or ''))


def number_key(card) -> str:
    """The ח"פ or ת"ז as it is compared: digits, without the zeros an ID loses in front. '' for none."""
    for raw in (card.company_number, card.id_number):
        digits = _digits(raw).lstrip('0')
        if len(digits) >= 5:
            return digits
    return ''


def phone_key(card) -> str:
    digits = _digits(card.phone)
    if digits.startswith('972') and len(digits) >= 11:
        digits = '0' + digits[3:]
    return digits if len(digits) >= 9 else ''


def last_seen(card, latest_document=None) -> date | None:
    """
    The newest day the customer is known to have been active: the last document
    kogo issued them, or the last one the previous software did (the import
    wrote it in the card's note). None when neither is known.
    """
    days = []
    if latest_document:
        days.append(latest_document)
    for day, month, year in _NOTE_DATE.findall(card.notes or ''):
        try:
            days.append(date(int(year), int(month), int(day)))
        except ValueError:
            continue
    return max(days) if days else None


def _location_label(card) -> str:
    parts = [
        card.business.name if card.business_id else '',
        card.business_category.name if card.business_category_id else '',
        card.branch.name if card.branch_id else '',
    ]
    return ' · '.join(part for part in parts if part)


def _card_dict(card, seen: date | None) -> dict:
    return {
        'id': str(card.pk),
        'name': card.full_name,
        'company_number': card.company_number or '',
        'id_number': card.id_number or '',
        'phone': card.phone or '',
        'email': card.email or '',
        'address': card.address or '',
        'location': _location_label(card),
        'documents': getattr(card, 'documents_count', 0),
        'tenancies': getattr(card, 'tenancies_count', 0),
        'last_seen': seen.isoformat() if seen else None,
    }


def suggested_name(cards: list, seen: dict) -> str:
    """
    The name the merged card would take. One customer under a card per branch
    keeps their name without the branch; a customer who changed name takes the
    newest one.
    """
    bases = {name_key(card.full_name) for card in cards}
    newest = max(cards, key=lambda card: (seen.get(card.pk) or date.min, card.updated_at))
    if len(bases) == 1:
        return without_branch(newest.full_name)
    return newest.full_name


def _looks_like_a_rename(members: list, seen: dict) -> bool:
    """Every card but the newest went quiet at least RENAME_GAP_DAYS before the newest one's last document."""
    days = sorted((seen.get(card.pk) for card in members if seen.get(card.pk)), reverse=True)
    if len(days) != len(members):
        return False
    return all((days[0] - older).days >= RENAME_GAP_DAYS for older in days[1:])


def _verdict(reason: str, members: list, seen: dict) -> tuple:
    """(recommended, hint): whether merging is what the office most likely wants, and why."""
    if reason == REASON_SAME_NAME:
        return True, 'אותו לקוח, עם כרטיס נפרד לכל סניף. הכרטיס המאוחד נקרא בלי הסניף.'
    if reason == REASON_SAME_NUMBER:
        if _looks_like_a_rename(members, seen):
            return True, 'אותו ח"פ, והשם הישן לא בשימוש כבר חצי שנה ויותר — נראה כמו שינוי שם. הכרטיס המאוחד מקבל את השם העדכני.'
        return False, 'אותו ח"פ, אבל שני השמות פעילים במקביל — ייתכן שאלה לקוחות נפרדים (למשל מתנ"סים של אותה רשת).'
    return False, 'אותו טלפון, אבל פרטים מזהים שונים — כדאי לבדוק אם זה באמת אותו לקוח.'


def suggest_groups() -> list:
    """
    Groups of cards that may be one customer, the surest first. Each:
    {reason, reason_label, recommended, hint, suggested_name, suggested_survivor_id, cards}.

    `recommended` where a merge is what it looks like on its face: the same
    name without the branch, or one number whose older name went out of use.
    The rest is shown to be looked at. A group the office already said is
    different customers is not shown again.
    """
    from apps.customers.models import BusinessCustomer

    cards = list(
        BusinessCustomer.objects.select_related('business', 'business_category', 'branch')
        .annotate(
            documents_count=Count('formal_documents', distinct=True),
            tenancies_count=Count('tenancies', distinct=True),
            latest_document=Max('formal_documents__document_date'),
        )
    )
    seen = {card.pk: last_seen(card, card.latest_document) for card in cards}

    by_reason = {reason: defaultdict(list) for reason in REASON_ORDER}
    for card in cards:
        name = name_key(card.full_name)
        if name:
            by_reason[REASON_SAME_NAME][name].append(card)
        number = number_key(card)
        if number:
            by_reason[REASON_SAME_NUMBER][number].append(card)
        phone = phone_key(card)
        if phone:
            by_reason[REASON_SAME_PHONE][phone].append(card)

    apart = [
        set(ids) for ids in
        BusinessCustomerCleanup.objects.filter(action=BusinessCustomerCleanup.ACTION_KEPT_APART)
        .values_list('card_ids', flat=True)
    ]

    groups, listed = [], set()
    for reason in REASON_ORDER:
        for members in by_reason[reason].values():
            if len(members) < 2:
                continue
            ids = frozenset(str(card.pk) for card in members)
            # Cards already shown together under a surer reason are not shown again
            # for a weaker one — the same group, or a part of it.
            if any(ids <= shown for shown in listed) or any(ids <= decided for decided in apart):
                continue
            listed.add(ids)
            ordered = sorted(
                members, key=lambda card: (seen.get(card.pk) or date.min, card.full_name), reverse=True,
            )
            survivor = max(
                members,
                key=lambda card: (card.documents_count + card.tenancies_count, seen.get(card.pk) or date.min),
            )
            recommended, hint = _verdict(reason, members, seen)
            groups.append({
                'reason': reason,
                'reason_label': REASON_LABELS[reason],
                'recommended': recommended,
                'hint': hint,
                'suggested_name': suggested_name(members, seen),
                'suggested_survivor_id': str(survivor.pk),
                'cards': [_card_dict(card, seen.get(card.pk)) for card in ordered],
            })
    return groups


# --------------------------------------------------------------------------
# Deciding
# --------------------------------------------------------------------------

def _actor_name(user) -> str:
    if user is None or not getattr(user, 'is_authenticated', False):
        return ''
    return (user.get_full_name() or user.get_username() or '')[:150]


def _snapshot(card) -> dict:
    """Every field of a card that is about to go away, as it can be kept and read."""
    values = {'name': card.full_name}
    for field in card._meta.concrete_fields:
        value = getattr(card, field.attname)
        values[field.attname] = value.isoformat() if isinstance(value, (date, datetime)) else (
            str(value) if value is not None and not isinstance(value, (str, int, bool)) else value
        )
    return values


def split_name(name: str) -> tuple:
    """A typed name as the card's two fields: the first word, then the rest."""
    words = _SPACES.sub(' ', (name or '').strip()).split(' ')
    first, last = words[0] if words and words[0] else '', ' '.join(words[1:])
    return first[:100], last[:100]


# The details a merged card fills from the ones that go away, where it has none.
_FILLED = ('email', 'phone', 'address', 'company_number', 'id_number')


def merge(survivor, others, *, name: str | None = None, user=None) -> dict:
    """
    Make `others` part of `survivor`. Returns {customer_id, name, merged, moved}.

    `name` renames the survivor (the name without the branch, or the newest
    one); None keeps its name. Raises CleanupError.
    """
    from apps.customers.models import BusinessCustomer
    from apps.legacy_import.models import LegacyImport

    other_ids = {card.pk for card in others}
    if not other_ids:
        raise CleanupError('יש לבחור לפחות כרטיס אחד לאיחוד')
    if survivor.pk in other_ids:
        raise CleanupError('הכרטיס שנשאר אינו יכול להיות גם כרטיס שמאוחד לתוכו')
    if name is not None and not name.strip():
        raise CleanupError('השם של הכרטיס המאוחד אינו יכול להיות ריק')

    with transaction.atomic():
        locked = {
            card.pk: card for card in
            BusinessCustomer.objects.select_for_update().filter(pk__in=other_ids | {survivor.pk})
        }
        if len(locked) != len(other_ids) + 1:
            raise CleanupError('אחד הכרטיסים כבר לא קיים. רעננו את הרשימה.')
        survivor = locked[survivor.pk]
        # The newest first: where two of them know a detail, the newer one's is taken.
        merged = sorted(
            (locked[pk] for pk in other_ids),
            key=lambda card: (last_seen(card) or date.min, card.updated_at), reverse=True,
        )
        name_before = survivor.full_name
        snapshots = [_snapshot(card) for card in merged]

        # Everything that points at a card going away now points at the one that stays.
        moved = {}
        for relation in BusinessCustomer._meta.related_objects:
            field = relation.field.name
            count = relation.related_model._default_manager.filter(
                **{f'{field}__in': other_ids},
            ).update(**{field: survivor})
            if count:
                moved[relation.related_model._meta.label] = count

        changed = set()
        for card in merged:
            for field in _FILLED:
                if not (getattr(survivor, field) or '').strip() and (getattr(card, field) or '').strip():
                    setattr(survivor, field, getattr(card, field))
                    changed.add(field)
            # A card with no location takes the newest one any of them had, whole.
            if not (survivor.business_id or survivor.branch_id) and (card.business_id or card.branch_id):
                for field in ('business', 'business_type', 'business_category', 'category', 'branch'):
                    setattr(survivor, field, getattr(card, field))
                    changed.add(field)
            # Consent to documents by email is the customer's, whichever card recorded it.
            if (not survivor.computerized_docs_consent_at and card.computerized_docs_consent_at
                    and not card.computerized_docs_consent_revoked_at):
                survivor.computerized_docs_consent_at = card.computerized_docs_consent_at
                survivor.computerized_docs_consent_source = card.computerized_docs_consent_source
                changed.update({'computerized_docs_consent_at', 'computerized_docs_consent_source'})
        if name is not None:
            survivor.first_name, survivor.last_name = split_name(name)
            changed.update({'first_name', 'last_name'})
        survivor.save(update_fields=sorted(changed | {'updated_at'}))

        # The import remembers which card each of the previous software's
        # customers became; the ones that went away are now the survivor.
        redirect = {str(pk): str(survivor.pk) for pk in other_ids}
        for legacy_import in LegacyImport.objects.exclude(result={}).only('id', 'result'):
            remembered = (legacy_import.result or {}).get('document_less_cards') or {}
            if not any(card_id in redirect for card_id in remembered.values()):
                continue
            legacy_import.result['document_less_cards'] = {
                key: redirect.get(card_id, card_id) for key, card_id in remembered.items()
            }
            legacy_import.save(update_fields=['result'])

        BusinessCustomerCleanup.objects.create(
            action=BusinessCustomerCleanup.ACTION_MERGED,
            survivor=survivor,
            card_ids=sorted([str(survivor.pk), *redirect]),
            merged_cards=snapshots,
            moved=moved,
            name_before=name_before[:210],
            name_after=survivor.full_name[:210],
            decided_by=user if _actor_name(user) else None,
            decided_by_name=_actor_name(user),
        )
        BusinessCustomer.objects.filter(pk__in=other_ids).delete()

    return {
        'customer_id': str(survivor.pk),
        'name': survivor.full_name,
        'merged': len(other_ids),
        'moved': moved,
    }


def keep_apart(cards, *, user=None) -> BusinessCustomerCleanup:
    """The office looked: these are different customers. They are not suggested together again."""
    ids = sorted({str(card.pk) for card in cards})
    if len(ids) < 2:
        raise CleanupError('יש לבחור לפחות שני כרטיסים')
    return BusinessCustomerCleanup.objects.create(
        action=BusinessCustomerCleanup.ACTION_KEPT_APART,
        card_ids=ids,
        decided_by=user if _actor_name(user) else None,
        decided_by_name=_actor_name(user),
    )


def office_named_cards() -> set:
    """The cards whose name the office chose in a merge: an import must not rename them back."""
    return set(
        BusinessCustomerCleanup.objects.filter(
            action=BusinessCustomerCleanup.ACTION_MERGED, survivor__isnull=False,
        ).values_list('survivor_id', flat=True)
    )
