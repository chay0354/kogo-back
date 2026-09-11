"""Tenancies and their contracts — /api/v1/rentals/.

    GET, POST          tenancies/                   list (branch, status, search) / create
    GET, PATCH, DELETE tenancies/{id}/              one tenancy; DELETE only a draft with no slots and no contracts
    POST               tenancies/{id}/link-slots/   {"slot_ids": [...]}, all of them or none
    POST               tenancies/{id}/unlink-slot/  {"slot_id": "..."}
    GET, POST          tenancies/{id}/contracts/    its contracts, newest first / issue the next version
    GET                tenancies/suggestions/       unlinked studio rentals, grouped by renter
    POST               tenancies/import/            {"groups": [...]}, in one transaction
    GET                contracts/{id}/pdf/          the stored PDF, checked against its fingerprint first
    POST               contracts/{id}/void/         {"reason": "..."}, a draft, sent or viewed contract
    POST               contracts/{id}/signing-link/         a new signing link for the current open contract
    POST               contracts/{id}/signing-link/cancel/  withdraw it; the contract is a draft again
    GET                contracts/{id}/signed-pdf/   the signed copy, checked against its fingerprint first

Managers and partners only. A partner reads and writes only the tenancies of
their own branches and those tenancies' contracts; one with no branches
assigned sees none. The tenant's own page, with no login, is public_views.py.
"""
from __future__ import annotations

import logging
import re
import uuid

from django.db.models import Prefetch, Q
from django.http import HttpResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import IsManagerOrPartner
from apps.core.scoping import scope_branches
from apps.customers.phone_search import phone_query_digits
from apps.rentals import slots as slot_rules
from apps.rentals.contracts import (
    HEAVY_COLUMNS,
    ContractError,
    issue_contract,
    live_contracts_prefetch,
    void_contract,
)
from apps.rentals.importer import GroupError, import_tenancies
from apps.rentals.models import RentalContract, Tenancy
from apps.rentals.serializers import RentalContractSerializer, SuggestionSerializer, TenancySerializer
from apps.rentals.signing import cancel_signing_link, issue_signing_link
from apps.scheduling.models import ScheduleEvent

logger = logging.getLogger(__name__)

_NON_DIGITS = re.compile(r'\D+')


def contracts_for_display():
    """Contracts as the office lists them: with the issuer and the signer, without the heavy columns."""
    return (
        RentalContract.objects.select_related('created_by', 'signature')
        .defer(*HEAVY_COLUMNS, 'signature__signature_png', 'signature__document_html')
    )


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


def _contract_error(exc: ContractError) -> Response:
    return Response({'error': exc.message}, status=status.HTTP_400_BAD_REQUEST)


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
            ),
            # current_contract and its is_stale, without a query per tenancy.
            live_contracts_prefetch(),
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
        """Only a draft that holds no slots and has no contracts. Anything further along is part of the record."""
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
        if tenancy.contracts.exists():
            # PROTECT would refuse the delete anyway; this says why instead of a 500.
            return Response(
                {'error': 'להסכם הזה כבר הופקו חוזים, ולכן אי אפשר למחוק אותו'},
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

    @action(detail=True, methods=['get', 'post'])
    def contracts(self, request, pk=None):
        """
        GET: the tenancy's contracts, newest first.
        POST: issue the next version from the tenancy as it is now — 201 with the
        new contract, or 400 with the reason none can be issued.
        """
        tenancy = self.get_object()
        # The request builds each live contract's signing URL (signing_url).
        context = {'request': request}
        if request.method == 'POST':
            try:
                contract = issue_contract(tenancy, request.user)
            except ContractError as exc:
                return _contract_error(exc)
            return Response(RentalContractSerializer(contract, context=context).data, status=status.HTTP_201_CREATED)
        contracts = contracts_for_display().filter(tenancy=tenancy).order_by('-version')
        return Response(RentalContractSerializer(contracts, many=True, context=context).data)

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


class RentalContractViewSet(viewsets.GenericViewSet):
    """
    חוזי שכירות — one issued contract: download its stored PDF or its signed
    copy, void it, or send it for signing (and withdraw the link).

    There is no list or edit here. A tenancy lists and issues its own contracts
    (TenancyViewSet.contracts), and a contract is never edited.
    """

    serializer_class = RentalContractSerializer
    permission_classes = [IsAuthenticated, IsManagerOrPartner]
    pagination_class = None
    filter_backends = []
    lookup_value_regex = '[0-9a-fA-F-]{36}'

    # The one heavy column each download reads; everything else leaves them all in the database.
    _READS = {'pdf': 'pdf', 'signed_pdf': 'signed_pdf'}

    def get_queryset(self):
        reads = self._READS.get(self.action)
        queryset = (
            RentalContract.objects.select_related('created_by', 'signature')
            .defer(*(column for column in HEAVY_COLUMNS if column != reads))
            .defer('signature__signature_png', 'signature__document_html')
        )
        # A partner reaches the contracts of their own branches' tenancies only.
        return scope_branches(queryset, self.request.user, 'tenancy__branch')

    def _pdf_response(self, contract, data, intact: bool, filename: str, what: str):
        """The stored file as a download. A file that no longer matches its fingerprint is never served."""
        if not intact:
            logger.error(
                'Rental contract %s (tenancy %s, version %s): the stored %s does not match its '
                'SHA-256; refusing to serve it',
                contract.pk, contract.tenancy_id, contract.version, what,
            )
            return Response(
                {'error': 'קובץ החוזה השמור אינו תקין ולכן לא הורד. יש לפנות לתמיכה'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        response = HttpResponse(bytes(data), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    @action(detail=True, methods=['get'])
    def pdf(self, request, pk=None):
        """The PDF exactly as it was issued."""
        contract = self.get_object()
        return self._pdf_response(
            contract, contract.pdf, contract.pdf_is_intact(), f'rental-contract-v{contract.version}.pdf', 'PDF',
        )

    @action(detail=True, methods=['get'], url_path='signed-pdf')
    def signed_pdf(self, request, pk=None):
        """The signed copy exactly as it was signed. A contract that is not signed has none."""
        contract = self.get_object()
        if contract.status != RentalContract.STATUS_SIGNED:
            return Response({'error': 'לחוזה הזה אין עותק חתום'}, status=status.HTTP_404_NOT_FOUND)
        return self._pdf_response(
            contract, contract.signed_pdf, contract.signed_pdf_is_intact(),
            f'rental-contract-v{contract.version}-signed.pdf', 'signed copy',
        )

    @action(detail=True, methods=['post'], url_path='signing-link')
    def signing_link(self, request, pk=None):
        """A new signing link — the first, or one that retires the last. The contract comes back with its URL."""
        contract = self.get_object()
        try:
            linked = issue_signing_link(contract)
        except ContractError as exc:
            return _contract_error(exc)
        return Response(self.get_serializer(linked).data)

    @action(detail=True, methods=['post'], url_path='signing-link/cancel')
    def cancel_signing_link(self, request, pk=None):
        """Withdraw the signing link. The URL stops working and the contract is a draft again."""
        contract = self.get_object()
        try:
            withdrawn = cancel_signing_link(contract)
        except ContractError as exc:
            return _contract_error(exc)
        return Response(self.get_serializer(withdrawn).data)

    @action(detail=True, methods=['post'])
    def void(self, request, pk=None):
        """Void a draft, sent or viewed contract: {"reason": "..."}. A signed or void one is refused."""
        contract = self.get_object()
        reason = request.data.get('reason', '') if isinstance(request.data, dict) else ''
        try:
            voided = void_contract(contract, reason)
        except ContractError as exc:
            return _contract_error(exc)
        return Response(self.get_serializer(voided).data)
