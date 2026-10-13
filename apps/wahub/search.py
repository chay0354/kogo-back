"""
The one search of "וואטסאפ ולידים" (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ח.1).

The owner, 13.10.2026: "when I look for a phone number — that it really shows
every number there is, or by name; that it is very easy to search."

What is typed is read word by word, and every word has to be found — in any
order, each in whatever field has it:

    a phone    four digits or more, however they were typed (050…, 972…,
               +972 50 123 4567, with dashes) and wherever they sit in the
               number: the contact's own, and — for a contact the system knows
               (`kogo_family_id`) — the family's, every parent's and extra
               contact's, every child's;
    a word     the contact's name; the family's name, the parents' and the
               children's; and, as before, the last message and the summary.
               Vowel points, quote marks, dashes, a final letter and double
               spaces make no difference, and a part of a word is enough.

The customers' tables are only read, with the two helpers they already have
for a phone typed any way (apps/customers/phone_search.py). The families a
word or a number belongs to are looked up once a word — one query each,
whatever the number of contacts — and the contacts are then filtered by their
ids: a list of conversations never joins the customers' tables.

`rank` orders what was found: by the contact's own name or phone first, then
through the family, then a word that only appears in the conversation.
`found_by` says, for a row that was not found by what it shows anyway, what it
was found by. `customers` are the families the search finds that no WhatsApp
contact stands for — "לקוח במערכת".
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from django.db.models import Case, CharField, Func, IntegerField, Q, Value, When
from django.db.models.functions import Lower

from apps.customers.child_status import STATUS_GHOST, canonical_status, status_label
from apps.customers.customer_details import SHARED_GHOST_FAMILY_NAME
from apps.customers.models import Child, Family, Parent
from apps.customers.phone_search import _digits_only
from apps.wahub.models import Contact
from apps.wahub.phones import normalize_phone, phone_display

MIN_PHONE_DIGITS = 4
MAX_WORDS = 6
MAX_PHONES = 2
# One letter is in every name: it is looked for in the contact's own name and words, not in the families.
MIN_FAMILY_WORD = 2
CUSTOMERS_SHOWN = 5
OTHERS_SHOWN = 5

RANK_OWN, RANK_FAMILY, RANK_TEXT = 0, 1, 2

# What a name is compared without: vowel points and cantillation (U+0591–U+05C7, the maqaf among
# them), quote marks of every kind, dots, commas and dashes; and a final letter reads as its plain one.
_DROPPED = ''.join(chr(code) for code in range(0x0591, 0x05C8)) + '\'"`´’‘“”׳״.,–—'
_DROP_PATTERN = '[' + _DROPPED + '-]'
_DROP = re.compile(_DROP_PATTERN)
_FINALS, _PLAIN = 'ךםןףץ', 'כמנפצ'
_TO_PLAIN = str.maketrans(_FINALS, _PLAIN)
_DIGITISH = re.compile(r'[\d+\-().]+')
_NON_DIGITS = re.compile(r'\D+')
_WORD_BREAK = re.compile(r'[-/־–—]+')

LABELS = {
    'child': 'ילד/ה',
    'parent': 'הורה',
    'family': 'שם המשפחה',
    'family_phone': 'טלפון נוסף של המשפחה',
    'phone': 'טלפון',
    'lesson': 'השיעור',
    'text': 'מופיע בשיחה',
}


def fold(text) -> str:
    """A name as it is compared: lower case, no points, quotes or dashes, plain final letters, single spaces."""
    return ' '.join(_DROP.sub('', str(text or '').lower()).translate(_TO_PLAIN).split())


def folded(field: str):
    """The same folding, in SQL, of a column."""
    dropped = Func(Lower(field), Value(_DROP_PATTERN), Value(''), Value('g'), function='REGEXP_REPLACE')
    return Func(dropped, Value(_FINALS), Value(_PLAIN), function='TRANSLATE', output_field=CharField())


@dataclass(frozen=True)
class Word:
    raw: str        # as typed: what a message or a summary is searched for
    folded: str     # what a name is compared with


@dataclass(frozen=True)
class Phone:
    digits: str

    @property
    def in_contact(self) -> tuple:
        """The spellings to look for in a contact's phone (kept as 9725XXXXXXXX)."""
        found = [self.digits]
        if self.digits.startswith('0'):
            found.append('972' + self.digits[1:])
        return tuple(dict.fromkeys(found))

    @property
    def in_customers(self) -> tuple:
        """The spellings to look for in a customer's phone (kept as it was typed, compared digits only)."""
        found = [self.digits]
        if self.digits.startswith('972') and len(self.digits) > 3:
            found.append('0' + self.digits[3:])
        if self.digits.startswith('0'):
            found.append('972' + self.digits[1:])
            if len(self.digits) >= 8:
                # A whole number kept without its leading zero.
                found.append(self.digits[1:])
        return tuple(dict.fromkeys(found))

    def hits(self, stored) -> bool:
        digits = _NON_DIGITS.sub('', str(stored or ''))
        return bool(digits) and any(spelling in digits for spelling in self.in_customers)


def parse(term) -> list:
    """
    What was typed, as the words and the numbers to find. Runs of digits that
    stand side by side are one number ("+972 50 123 4567"); fewer than four
    digits are a word like any other.
    """
    tokens, run = [], ''

    def close_run():
        nonlocal run
        if not run:
            return
        if run.startswith('00972'):
            run = run[2:]
        tokens.append(Phone(run) if len(run) >= MIN_PHONE_DIGITS else Word(run, run))
        run = ''

    for piece in str(term or '').split():
        if _DIGITISH.fullmatch(piece) and any(char.isdigit() for char in piece):
            run += _NON_DIGITS.sub('', piece)
            continue
        close_run()
        for part in _WORD_BREAK.split(piece):
            word = fold(part)
            if word:
                tokens.append(Word(part, word))
    close_run()
    words = [token for token in tokens if isinstance(token, Word)][:MAX_WORDS]
    phones = [token for token in tokens if isinstance(token, Phone)][:MAX_PHONES]
    return [token for token in tokens if token in words or token in phones]


def _any(pairs) -> Q:
    """OR of field lookups; a condition nothing meets when there is none."""
    cond = Q(pk__in=[])
    for name, value in pairs:
        cond |= Q(**{name: value})
    return cond


def families_of(token) -> set:
    """
    The families a word or a number belongs to — by the family's own name and
    phone, by any parent (the extra contacts are parents), by any child that is
    not a walk-in. One query.
    """
    if isinstance(token, Phone):
        spellings = token.in_customers
        family = Family.objects.alias(_d=_digits_only('phone')).filter(_any(('_d__contains', value) for value in spellings))
        parent = Parent.objects.alias(_d=_digits_only('phone')).filter(_any(('_d__contains', value) for value in spellings))
        child = Child.objects.exclude(status=STATUS_GHOST).alias(_d=_digits_only('phone_number')).filter(
            _any(('_d__contains', value) for value in spellings)
        )
    else:
        if len(token.folded) < MIN_FAMILY_WORD:
            return set()
        word = token.folded
        family = Family.objects.alias(_n=folded('name')).filter(_n__contains=word)
        parent = Parent.objects.alias(_f=folded('first_name'), _l=folded('last_name')).filter(Q(_f__contains=word) | Q(_l__contains=word))
        child = Child.objects.exclude(status=STATUS_GHOST).alias(_f=folded('first_name'), _l=folded('last_name')).filter(
            Q(_f__contains=word) | Q(_l__contains=word)
        )
    return set(
        family.values_list('id', flat=True).union(
            parent.values_list('family_id', flat=True), child.values_list('family_id', flat=True),
        )
    )


class Search:
    """One typed search, applied to any list of contacts. Nothing is asked of the database until it is used."""

    TEXT_FIELDS = ('last_message_text', 'known_summary')

    def __init__(self, term):
        self.tokens = parse(term)
        self._families: list | None = None

    def __bool__(self) -> bool:
        return bool(self.tokens)

    @property
    def families(self) -> list:
        """For each token, the ids of the families it belongs to."""
        if self._families is None:
            self._families = [families_of(token) for token in self.tokens]
        return self._families

    # --- conditions on a contact ---

    @staticmethod
    def _own(token) -> Q:
        if isinstance(token, Phone):
            return _any(('phone__contains', value) for value in token.in_contact)
        return Q(_search_name__contains=token.folded)

    def _identity(self, index: int) -> Q:
        ids = self.families[index]
        return self._own(self.tokens[index]) | (Q(kogo_family_id__in=ids) if ids else Q(pk__in=[]))

    def _anywhere(self, index: int, text_fields) -> Q:
        cond = self._identity(index)
        token = self.tokens[index]
        if isinstance(token, Word):
            for name in text_fields:
                cond |= Q(**{f'{name}__icontains': token.raw})
        return cond

    def _all(self, make) -> Q:
        cond = Q()
        for index in range(len(self.tokens)):
            cond &= make(index)
        return cond

    def apply(self, contacts, *, extra_text=(), rank: bool = True, identity_only: bool = False):
        """
        The contacts the search finds, each with `_search_rank` (0 — by its own
        name or phone, 1 — through the family, 2 — a word in the conversation).
        `identity_only` leaves out whoever only has the word in a text.
        """
        if not self.tokens:
            return contacts
        text_fields = (*self.TEXT_FIELDS, *extra_text)
        contacts = contacts.alias(_search_name=folded('name'))
        identity = self._all(self._identity)
        contacts = contacts.filter(identity if identity_only else self._all(lambda index: self._anywhere(index, text_fields)))
        if rank:
            contacts = contacts.annotate(_search_rank=Case(
                When(self._all(lambda index: self._own(self.tokens[index])), then=Value(RANK_OWN)),
                When(identity, then=Value(RANK_FAMILY)),
                default=Value(RANK_TEXT), output_field=IntegerField(),
            ))
        return contacts

    def contacts_with(self, index: int, *, extra_text=()):
        """The ids of the contacts one token is found in — for a list that is not a list of contacts."""
        text_fields = (*self.TEXT_FIELDS, *extra_text)
        return Contact.objects.alias(_search_name=folded('name')).filter(self._anywhere(index, text_fields)).values('id')

    # --- why a row is there ---

    def _own_hit(self, token, contact) -> bool:
        if isinstance(token, Phone):
            return any(value in (contact.phone or '') for value in token.in_contact)
        return token.folded in fold(contact.name)

    def found_by(self, contacts) -> dict:
        """
        {contact id: None | {kind, label, value}} for rows in hand. None — found
        by the contact's own name or phone, which the row shows anyway. The
        families' people are read once for all the rows that need them.
        """
        contacts = list(contacts)
        if not self.tokens:
            return {}
        missing = {
            contact.pk: [token for token in self.tokens if not self._own_hit(token, contact)] for contact in contacts
        }
        wanted = {contact.kogo_family_id for contact in contacts if missing[contact.pk] and contact.kogo_family_id}
        people = _people(wanted)
        found = {}
        for contact in contacts:
            if not missing[contact.pk]:
                found[contact.pk] = None
                continue
            family = people.get(contact.kogo_family_id)
            found[contact.pk] = (_family_hit(missing[contact.pk], family) if family else None) or _said('text', '')
        return found

    # --- the customers no contact stands for ---

    def customers(self, *, limit: int = CUSTOMERS_SHOWN) -> tuple:
        """
        (the families the search finds that no WhatsApp contact is matched to —
        newest first, `limit` at most; whether there are more). Reads only.
        """
        if not self.tokens or any(not ids for ids in self.families):
            return [], False
        ids = set.intersection(*self.families)
        if not ids:
            return [], False
        behind_a_contact = Contact.objects.filter(kogo_family_id__isnull=False).values('kogo_family_id')
        rows = list(
            Family.objects.filter(id__in=ids).exclude(name=SHARED_GHOST_FAMILY_NAME).exclude(id__in=behind_a_contact)
            .prefetch_related('parents', 'children').order_by('-created_at', 'id')[:limit * 3 + 1]
        )
        shown = []
        for family in rows:
            children = [child for child in family.children.all() if child.status != STATUS_GHOST]
            if not children and list(family.children.all()):
                continue        # only walk-ins: a note an instructor made, nobody's card
            shown.append((family, list(family.parents.all()), children))
        more = len(shown) > limit or len(rows) > limit * 3
        shown = shown[:limit]
        # A contact with one of these phones that the matching has not reached yet.
        phones = {}
        for family, parents, children in shown:
            for raw in [family.phone, *(parent.phone for parent in parents), *(child.phone_number for child in children)]:
                phone = normalize_phone(raw)
                if phone:
                    phones.setdefault(phone, family.id)
        writes = {}
        if phones:
            for phone, contact_id in Contact.objects.filter(phone__in=list(phones)).values_list('phone', 'id'):
                writes.setdefault(phones[phone], contact_id)
        return [self._customer(family, parents, children, writes.get(family.id)) for family, parents, children in shown], more

    def _customer(self, family, parents, children, contact_id) -> dict:
        primary = next((parent for parent in parents if parent.is_primary), parents[0] if parents else None)
        name = f'{primary.first_name} {primary.last_name}'.strip() if primary is not None else ''
        said = _family_hit(self.tokens, _Family(family, parents, children), own=True)
        first = children[0] if children else None
        return {
            'family_id': str(family.id),
            'name': name or family.name,
            'family_name': family.name,
            'phone': family.phone,
            'phone_display': _shown_phone(family.phone),
            'children': [_child(child) for child in children],
            'child_id': str(first.id) if first is not None else None,
            'link': customer_link(first.id if first is not None else None),
            'contact_id': contact_id,
            'found_by': said,
        }


def customer_link(child_id) -> str | None:
    """The customer's card, as the rest of the section links to it; None when there is no child to open."""
    return f'/customers?child={child_id}' if child_id else None


def customer_block(contact: Contact, *, child_id=None) -> dict | None:
    """
    `customer` of a contact, a lead and a waiting-list row (the contract, ח.2):
    the family the system matched the contact to, the child whose card opens,
    and the address of that card. None for a contact the system does not know.
    """
    if not contact.kogo_family_id:
        return None
    children = [str(value) for value in (contact.kogo_child_ids or [])]
    chosen = str(child_id) if child_id else (children[0] if children else None)
    return {'family_id': str(contact.kogo_family_id), 'child_id': chosen, 'link': customer_link(chosen)}


def _shown_phone(raw) -> str:
    return phone_display(normalize_phone(raw) or str(raw or ''))


def _child(child) -> dict:
    status = canonical_status(child.status) or child.status
    return {
        'id': str(child.id),
        'name': f'{child.first_name} {child.last_name}'.strip(),
        'status': status,
        'status_label': status_label(status),
    }


def _said(kind: str, value: str) -> dict:
    return {'kind': kind, 'label': LABELS[kind], 'value': value}


@dataclass
class _Family:
    family: object
    parents: list
    children: list


def _people(family_ids) -> dict:
    """{family id: its people} for the families given — three queries, whatever their number."""
    family_ids = [family_id for family_id in family_ids if family_id]
    if not family_ids:
        return {}
    found = {family.id: _Family(family, [], []) for family in Family.objects.filter(id__in=family_ids).only('id', 'name', 'phone')}
    for parent in Parent.objects.filter(family_id__in=found).only('id', 'family_id', 'first_name', 'last_name', 'phone', 'is_primary'):
        found[parent.family_id].parents.append(parent)
    for child in Child.objects.filter(family_id__in=found).exclude(status=STATUS_GHOST).only(
        'id', 'family_id', 'first_name', 'last_name', 'phone_number', 'status',
    ):
        found[child.family_id].children.append(child)
    return found


def _family_hit(tokens, person: _Family, *, own: bool = False) -> dict | None:
    """
    What of a family the tokens are found in, said for a row in one line: a
    phone, when a number was typed; else the family's name, when that alone
    answers every word; else the child — or the parent — whose name answers
    the most of them. `own` — the row IS the family (a customer with no
    conversation), so its phone is "טלפון", not "another phone".
    """
    for token in tokens:
        if isinstance(token, Phone):
            for raw in [person.family.phone, *(parent.phone for parent in person.parents), *(child.phone_number for child in person.children)]:
                if token.hits(raw):
                    return _said('phone' if own else 'family_phone', _shown_phone(raw))
    words = [token.folded for token in tokens if isinstance(token, Word) and len(token.folded) >= MIN_FAMILY_WORD]
    if not words:
        return None
    family_name = fold(person.family.name)
    if all(word in family_name for word in words):
        return _said('family', person.family.name)
    best, best_score = None, 0
    for kind, people in (('child', person.children), ('parent', person.parents)):
        for one in people:
            first, last = fold(one.first_name), fold(one.last_name)
            score = sum(1 for word in words if word in first or word in last)
            if score > best_score:
                best, best_score = _said(kind, f'{one.first_name} {one.last_name}'.strip()), score
    if best is not None:
        return best
    return _said('family', person.family.name) if any(word in family_name for word in words) else None
