"""Search that finds a phone however it was typed or stored."""
import re

from django.db.models import F, Q, Value
from django.db.models.functions import Replace
from rest_framework import filters

PHONE_MIN_DIGITS = 7
_NON_DIGITS = re.compile(r'\D+')


def phone_query_digits(term: str) -> str:
    """'+972 52-265-9322' -> '0522659322'; anything that is not a phone -> ''."""
    digits = _NON_DIGITS.sub('', term)
    if len(digits) < PHONE_MIN_DIGITS or digits != term.replace('+', '').replace('-', '').replace(' ', ''):
        return ''
    if digits.startswith('972'):
        digits = '0' + digits[3:]
    return digits


def _digits_only(field: str):
    expr = F(field)
    for char in ('-', ' ', '+', '(', ')'):
        expr = Replace(expr, Value(char), Value(''))
    return expr


class PhoneAwareSearchFilter(filters.SearchFilter):
    """
    DRF's SearchFilter, except that a term that looks like a phone number is
    compared digit-for-digit against the phone fields, with a 972 prefix read
    as the leading 0. Other terms behave exactly as before.
    """

    def filter_queryset(self, request, queryset, view):
        search_fields = self.get_search_fields(view, request)
        terms = self.get_search_terms(request)
        if not search_fields or not terms:
            return queryset
        # '+972 52-265-9322' is one phone, not three words.
        raw = request.query_params.get(self.search_param, '')
        if phone_query_digits(raw):
            terms = [raw]

        phone_fields = [f for f in search_fields if f.split('__')[-1].startswith('phone')]
        id_fields = [f for f in search_fields if f.split('__')[-1].endswith('id_number')]
        annotations = {f'_digits_{i}': _digits_only(f) for i, f in enumerate(phone_fields)}
        if annotations:
            queryset = queryset.annotate(**annotations)

        for term in terms:
            digits = phone_query_digits(term)
            if digits:
                cond = Q()
                for alias in annotations:
                    cond |= Q(**{f'{alias}__icontains': digits})
                    # A stored 972 number is the same phone.
                    cond |= Q(**{f'{alias}__icontains': '972' + digits[1:]})
                # A long run of digits may just as well be an ID number.
                raw_digits = _NON_DIGITS.sub('', term)
                for field in id_fields:
                    cond |= Q(**{f'{field}__icontains': raw_digits})
            else:
                cond = Q()
                for field in search_fields:
                    cond |= Q(**{f'{field}__icontains': term})
            queryset = queryset.filter(cond)
        return queryset.distinct() if self.must_call_distinct(queryset, search_fields) else queryset


def _phone_hit(stored: str, digits: str) -> bool:
    if not stored or not digits:
        return False
    stored_digits = _NON_DIGITS.sub('', stored)
    return digits in stored_digits or ('972' + digits[1:]) in stored_digits


def describe_search_match(child, term: str):
    """
    Which field a child matched on, when it is one the list does not show.

    The list shows the child's name and one phone (the child's own, else the
    primary parent's). A hit on anything else — a parent's name, another
    phone, an ID number, the family name — is returned as a label and the
    value that matched, so the row can say why it is there. None means the
    match is already visible in the row.
    """
    term = (term or '').strip()
    if not term:
        return None
    text = term.casefold()
    digits = phone_query_digits(term)
    raw_digits = _NON_DIGITS.sub('', term)
    family = getattr(child, 'family', None)
    parents = list(family.parents.all()) if family is not None else []
    primary = next((p for p in parents if getattr(p, 'is_primary', False)), parents[0] if parents else None)

    # Already on the row: the child's name, and the phone the row displays.
    if text in f'{child.first_name} {child.last_name}'.casefold():
        return None
    shown_phone = child.phone_number or (primary.phone if primary else '') or (family.phone if family else '')
    if digits and _phone_hit(shown_phone, digits):
        return None

    for parent in parents:
        full = f'{parent.first_name} {parent.last_name}'.strip()
        if text in full.casefold():
            return {'label': 'שם הורה', 'value': full}
    for parent in parents:
        if digits and _phone_hit(parent.phone, digits):
            return {'label': 'טלפון הורה', 'value': parent.phone}
    if family is not None and digits and _phone_hit(family.phone, digits):
        return {'label': 'טלפון משפחה', 'value': family.phone}
    if raw_digits and child.id_number and raw_digits in child.id_number:
        return {'label': 'ת.ז. ילד', 'value': child.id_number}
    if family is not None and raw_digits and family.parent_id_number and raw_digits in family.parent_id_number:
        return {'label': 'ת.ז. הורה', 'value': family.parent_id_number}
    if family is not None and family.name and text in family.name.casefold():
        return {'label': 'שם משפחה', 'value': family.name}
    return None
