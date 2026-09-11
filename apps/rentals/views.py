"""Tenancies API — /api/v1/rentals/tenancies/.

    GET, POST          tenancies/                   list (branch, status, search) / create
    GET, PATCH, DELETE tenancies/{id}/              one tenancy; DELETE only a draft with no slots
    POST               tenancies/{id}/link-slots/   {"slot_ids": [...]}, all of them or none
    POST               tenancies/{id}/unlink-slot/  {"slot_id": "..."}
    GET                tenancies/suggestions/       unlinked studio rentals, grouped by renter
    POST               tenancies/import/            {"groups": [...]}, in one transaction

Managers and partners only. A partner reads and writes only the tenancies of
their own branches, and one with no branches assigned sees none.
"""
from __future__ import annotations

import re
import uuid

from django.db.models import Prefetch, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import IsManagerOrPartner
from apps.core.scoping import scope_branches
from apps.customers.phone_search import phone_query_digits
from apps.rentals import slots as slot_rules
from apps.rentals.importer import GroupError, import_tenancies
from apps.rentals.models import Tenancy
from apps.rentals.serializers import SuggestionSerializer, TenancySerializer
from apps.scheduling.models import ScheduleEvent

_NON_DIGITS = re.compile(r'\D+')


def _search_tenancies(queryset, raw):
    """
    Search by the tenant: name, company number, ID number or phone.

    A term that is a number is compared digit for digit, so '51-234567-8'
    finds a company stored as '512345678', and '+972 52-265-9322' finds
    '052-2659322'. Any other text must appear in one of the fields, word by word.
    """
    raw = (raw or '').strip()
    if not raw:
        return queryset
    phone_digits = phone_query_digits(raw)
    if phone_digits:
        digits = _NON_DIGITS.sub('', raw)
        queryset = queryset.annotate(
            _phone_digits=slot_rules.DigitsOnly('tenant__phone'),
            _company_digits=slot_rules.DigitsOnly('tenant__company_number'),
            _id_digits=slot_rules.DigitsOnly('tenant__id_number'),
        )
        return queryset.filter(
            Q(_phone_digits__contains=phone_digits)
            | Q(_phone_digits__contains='972' + phone_digits[1:])
            | Q(_company_digits__contains=digits)
            | Q(_id_digits__contains=digits)
        )
    for term in raw.split():
        queryset = queryset.filter(
            Q(tenant__first_name__icontains=term)
            | Q(tenant__last_name__icontains=term)
            | Q(tenant__company_number__icontains=term)
            | Q(tenant__id_number__icontains=term)
            | Q(tenant__phone__icontains=term)
        )
    return queryset


def _slot_error(exc: slot_rules.SlotError) -> Response:
    body = {'error': exc.message}
    if exc.slot_id:
        body['slot_id'] = exc.slot_id
    return Response(body, status=status.HTTP_400_BAD_REQUEST)


class TenancyViewSet(viewsets.ModelViewSet):
    """הסכמי שכירות — one tenant, their studio slots, and the monthly terms."""

    serializer_class = TenancySerializer
    permission_classes = [IsAuthenticated, IsManagerOrPartner]
    # A studio has a few dozen tenants at most; the screen wants all of them.
    pagination_class = None
    # branch, status and search are read in get_queryset: search spans the tenant's fields.
    filter_backends = []
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']
    # Only an id reaches the detail route, so 'suggestions' and 'import' never look like one.
    lookup_value_regex = '[0-9a-fA-F-]{36}'

    def get_queryset(self):
        queryset = Tenancy.objects.select_related('tenant', 'branch').prefetch_related(
            Prefetch(
                'slots',
                queryset=ScheduleEvent.objects.select_related('branch', 'studio')
                .order_by('event_date', 'start_time', 'created_at'),
            )
        )
        # A partner reaches their own branches' tenancies only, none without a branch.
        queryset = scope_branches(queryset, self.request.user, 'branch')
        if self.action != 'list':
            return queryset

        params = self.request.query_params
        branch_id = params.get('branch')
        if branch_id and branch_id != 'all':
            try:
                queryset = queryset.filter(branch_id=uuid.UUID(str(branch_id)))
            except ValueError:
                raise ValidationError({'branch': 'מזהה סניף לא תקין'})
        statuses = [value for value in (params.get('status') or '').split(',') if value]
        if statuses:
            queryset = queryset.filter(status__in=statuses)
        return _search_tenancies(queryset, params.get('search'))

    def _read(self, tenancy_ids):
        """Tenancies as the API shows them, freshly read, in the order asked for."""
        by_pk = {tenancy.pk: tenancy for tenancy in self.get_queryset().filter(pk__in=tenancy_ids)}
        return [by_pk[pk] for pk in tenancy_ids]

    def destroy(self, request, *args, **kwargs):
        """Only a draft that holds no slots. Anything further along is part of the record."""
        tenancy = self.get_object()
        if tenancy.status != Tenancy.STATUS_DRAFT:
            return Response(
                {'error': 'אפשר למחוק רק הסכם שכירות בסטטוס טיוטה'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if tenancy.slots.exists():
            return Response(
                {'error': 'יש לנתק את השכירויות מההסכם לפני מחיקתו'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        tenancy.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'], url_path='link-slots')
    def link_slots(self, request, pk=None):
        """Attach studio rentals to this tenancy: {"slot_ids": [...]}. All of them, or none."""
        tenancy = self.get_object()
        slot_ids = request.data.get('slot_ids') if isinstance(request.data, dict) else None
        try:
            slot_rules.link_slots(tenancy, slot_ids, request.user)
        except slot_rules.SlotError as exc:
            return _slot_error(exc)
        (fresh,) = self._read([tenancy.pk])
        return Response(self.get_serializer(fresh).data)

    @action(detail=True, methods=['post'], url_path='unlink-slot')
    def unlink_slot(self, request, pk=None):
        """Let one slot go: {"slot_id": "..."}. The event stays on the calendar."""
        tenancy = self.get_object()
        slot_id = request.data.get('slot_id') if isinstance(request.data, dict) else None
        if not slot_id:
            return Response({'error': 'יש לבחור שכירות לניתוק'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            slot_rules.unlink_slot(tenancy, slot_id)
        except slot_rules.SlotError as exc:
            return _slot_error(exc)
        (fresh,) = self._read([tenancy.pk])
        return Response(self.get_serializer(fresh).data)

    @action(detail=False, methods=['get'])
    def suggestions(self, request):
        """The studio rentals no tenancy holds yet, one group per renter and branch."""
        groups = slot_rules.rental_suggestions(request.user)
        return Response(SuggestionSerializer(groups, many=True).data)

    @action(detail=False, methods=['post'], url_path='import')
    def import_groups(self, request):
        """
        The office's confirmed suggestions, as tenancies: {"groups": [...]}.

        All in one transaction. A group that fails rolls the whole import back,
        and the answer says which group failed and why.
        """
        groups = request.data.get('groups') if isinstance(request.data, dict) else None
        if not isinstance(groups, list) or not groups:
            return Response(
                {'error': 'יש לשלוח לפחות קבוצה אחת לייבוא'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            created = import_tenancies(groups, request)
        except GroupError as exc:
            return Response(exc.payload(), status=exc.status_code)
        tenancies = self._read([tenancy.pk for tenancy in created])
        return Response(self.get_serializer(tenancies, many=True).data, status=status.HTTP_201_CREATED)
