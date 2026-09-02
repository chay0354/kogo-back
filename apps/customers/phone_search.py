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
            else:
                cond = Q()
                for field in search_fields:
                    cond |= Q(**{f'{field}__icontains': term})
            queryset = queryset.filter(cond)
        return queryset.distinct() if self.must_call_distinct(queryset, search_fields) else queryset
