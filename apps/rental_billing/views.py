"""Tenant billing — /api/v1/rental-billing/.

Office — managers and partners. A partner reaches the orders and charges of
their own branches only (another branch's is 404); a worker is refused (403).

    GET, POST   standing-orders/                    list (?tenancy= ?status=a,b ?branch=) / open one from a tenancy
    GET, PATCH  standing-orders/{id}/               one order / amount_before_vat, billing_day, end_date, notes
    POST        standing-orders/{id}/pause/         active → paused
    POST        standing-orders/{id}/resume/        paused → active; the paused months are not charged
    POST        standing-orders/{id}/end/           → ended; its waiting card link is cancelled
    POST        standing-orders/{id}/card-link/     a new card link; the previous URL stops working
    GET         standing-orders/{id}/charges/       the order's charges, newest month first
    GET         charges/                            across orders (?status= ?branch= ?standing_order= ?tenancy=
                                                    ?period=YYYY-MM ?needs_receipt=1), newest 500
    GET         charges/{id}/
    POST        charges/{id}/retry/                 a failed month, now — refused while billing is off
    POST        charges/{id}/mark-charged/          {"transaction_id", "confirmation_code"?, "note"?} a month in review went through
    POST        charges/{id}/void/                  {"reason"} a month in review, or a failed one, is not charged
    POST        charges/{id}/issue-receipt/         the receipt of a charged month that has none
    GET         status/                             {"enabled", "business_name", "business_found", "business_id"}

Public — the tenant, no login, throttled:

    GET, POST   card/{token}/                       what the page shows / {"card_details": {...}}

Cron — the courses' cron auth (CRON_TOKEN / CRON_SECRET):

    POST        cron/charge/                        ?limit= ; not scheduled in vercel.json until phase 7
"""
from __future__ import annotations

import uuid
from datetime import date

from django.db.models import Prefetch
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.card_validation import CardValidationError, validate_card_details
from apps.core.models import BusinessCategory
from apps.core.permissions import IsManagerOrPartner
from apps.core.scoping import scope_branches
from apps.rental_billing import billing, orders
from apps.rental_billing.card import CardEntryError, apply_card, preview_payload, resolve_link
from apps.rental_billing.errors import DISABLED_MESSAGE, BillingDisabled, BillingError
from apps.rental_billing.links import rotate_card_link
from apps.rental_billing.models import TenantCardLink, TenantCharge, TenantStandingOrder
from apps.rental_billing.serializers import StandingOrderSerializer, TenantChargeSerializer, card_link_payload
from apps.rentals.models import BILLING_DAY_MAX, BILLING_DAY_MIN, Tenancy

UUID_REGEX = '[0-9a-fA-F-]{36}'
CHARGES_LIST_CAP = 500


def _billing_error(exc) -> Response:
    if isinstance(exc, BillingDisabled):
        return Response({'error': exc.message, 'disabled': True}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({'error': exc.message}, status=exc.status_code)


def _uuid_param(params, name):
    raw = params.get(name)
    if not raw or raw == 'all':
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        raise ValidationError({name: 'מזהה לא תקין'})


def _body(request) -> dict:
    return request.data if isinstance(request.data, dict) else {}


class _BillingDayField(serializers.IntegerField):
    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if not BILLING_DAY_MIN <= value <= BILLING_DAY_MAX:
            raise ValidationError(f'יום החיוב חייב להיות בין {BILLING_DAY_MIN} ל־{BILLING_DAY_MAX}')
        return value


class StandingOrderCreateInput(serializers.Serializer):
    tenancy_id = serializers.UUIDField()
    amount_before_vat = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    billing_day = _BillingDayField(required=False)
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    source = serializers.ChoiceField(choices=TenantStandingOrder.SOURCE_CHOICES, required=False)
    business_category_id = serializers.UUIDField(required=False, allow_null=True)


class StandingOrderUpdateInput(serializers.Serializer):
    amount_before_vat = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    billing_day = _BillingDayField(required=False)
    end_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=5000)


def _charges_queryset(user):
    queryset = TenantCharge.objects.select_related(
        'standing_order', 'standing_order__tenant', 'standing_order__branch',
        'business', 'business_category', 'receipt', 'resolved_by',
    )
    # A partner reaches the charges of their own branches' orders only.
    return scope_branches(queryset, user, 'standing_order__branch')


class StandingOrderViewSet(viewsets.GenericViewSet):
    """הוראות קבע של שוכרים."""

    serializer_class = StandingOrderSerializer
    permission_classes = [IsAuthenticated, IsManagerOrPartner]
    pagination_class = None
    filter_backends = []
    lookup_value_regex = UUID_REGEX

    def get_queryset(self):
        queryset = TenantStandingOrder.objects.select_related(
            'tenant', 'branch', 'business', 'business_category', 'created_by',
        ).prefetch_related(
            Prefetch('card_links', queryset=TenantCardLink.objects.order_by('-created_at'), to_attr='recent_card_links'),
        )
        # A partner reaches their own branches' orders only, none without a branch.
        return scope_branches(queryset, self.request.user, 'branch')

    def _read(self, order) -> dict:
        return self.get_serializer(self.get_queryset().get(pk=order.pk)).data

    def list(self, request):
        queryset = self.get_queryset()
        params = request.query_params
        tenancy_id = _uuid_param(params, 'tenancy')
        if tenancy_id:
            queryset = queryset.filter(tenancy_id=tenancy_id)
        branch_id = _uuid_param(params, 'branch')
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)
        statuses = [value for value in (params.get('status') or '').split(',') if value]
        if statuses:
            queryset = queryset.filter(status__in=statuses)
        return Response(self.get_serializer(queryset, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(self.get_serializer(self.get_object()).data)

    def create(self, request):
        """Open a standing order for a tenancy: {"tenancy_id", ...}. It waits for the tenant's card."""
        payload = StandingOrderCreateInput(data=_body(request))
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        tenancy = (
            scope_branches(Tenancy.objects.select_related('tenant', 'branch'), request.user, 'branch')
            .filter(pk=data['tenancy_id']).first()
        )
        if tenancy is None:
            return Response({'error': 'הסכם השכירות לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        category = None
        if data.get('business_category_id'):
            category = BusinessCategory.objects.filter(pk=data['business_category_id']).first()
            if category is None:
                return Response({'business_category_id': 'הקטגוריה לא נמצאה'}, status=status.HTTP_400_BAD_REQUEST)
        kwargs = {
            'user': request.user,
            'source': data.get('source', TenantStandingOrder.SOURCE_OFFICE),
            'amount_before_vat': data.get('amount_before_vat'),
            'billing_day': data.get('billing_day'),
            'start_date': data.get('start_date'),
            'notes': data.get('notes', ''),
            'business_category': category,
        }
        if 'end_date' in data:
            kwargs['end_date'] = data['end_date']
        try:
            order = orders.open_standing_order(tenancy, **kwargs)
        except BillingError as exc:
            return _billing_error(exc)
        return Response(self._read(order), status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        """amount_before_vat, billing_day, end_date, notes. The card, the status and the tenancy are never written here."""
        order = self.get_object()
        body = _body(request)
        unknown = sorted(set(body) - set(orders.EDITABLE_FIELDS))
        if unknown:
            return Response(
                {'error': f'אי אפשר לשנות את השדות: {", ".join(unknown)}'}, status=status.HTTP_400_BAD_REQUEST,
            )
        payload = StandingOrderUpdateInput(data=body, partial=True)
        payload.is_valid(raise_exception=True)
        try:
            orders.update_standing_order(order, dict(payload.validated_data))
        except BillingError as exc:
            return _billing_error(exc)
        return Response(self._read(order))

    def _lifecycle(self, change):
        order = self.get_object()
        try:
            change(order)
        except BillingError as exc:
            return _billing_error(exc)
        return Response(self._read(order))

    @action(detail=True, methods=['post'])
    def pause(self, request, pk=None):
        return self._lifecycle(orders.pause_order)

    @action(detail=True, methods=['post'])
    def resume(self, request, pk=None):
        return self._lifecycle(orders.resume_order)

    @action(detail=True, methods=['post'])
    def end(self, request, pk=None):
        return self._lifecycle(orders.end_order)

    @action(detail=True, methods=['post'], url_path='card-link')
    def card_link(self, request, pk=None):
        """A new card link for the order (pending_card or failed). 201 with its URL."""
        order = self.get_object()
        try:
            link = rotate_card_link(order, request.user)
        except BillingError as exc:
            return _billing_error(exc)
        return Response(card_link_payload(link, request), status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def charges(self, request, pk=None):
        order = self.get_object()
        queryset = _charges_queryset(request.user).filter(standing_order=order).order_by('-period', '-created_at')
        return Response(TenantChargeSerializer(queryset, many=True, context=self.get_serializer_context()).data)


class TenantChargeViewSet(viewsets.GenericViewSet):
    """חיובי שוכרים, and the office's decisions on them."""

    serializer_class = TenantChargeSerializer
    permission_classes = [IsAuthenticated, IsManagerOrPartner]
    pagination_class = None
    filter_backends = []
    lookup_value_regex = UUID_REGEX

    def get_queryset(self):
        return _charges_queryset(self.request.user)

    def _read(self, charge) -> dict:
        return self.get_serializer(self.get_queryset().get(pk=charge.pk)).data

    def list(self, request):
        queryset = self.get_queryset()
        params = request.query_params
        statuses = [value for value in (params.get('status') or '').split(',') if value]
        if statuses:
            queryset = queryset.filter(status__in=statuses)
        for param, lookup in (('branch', 'standing_order__branch_id'), ('standing_order', 'standing_order_id'),
                              ('tenancy', 'standing_order__tenancy_id')):
            value = _uuid_param(params, param)
            if value:
                queryset = queryset.filter(**{lookup: value})
        raw_period = (params.get('period') or '').strip()
        if raw_period:
            try:
                year, month = (int(part) for part in raw_period.split('-'))
                queryset = queryset.filter(period=date(year, month, 1))
            except (TypeError, ValueError):
                raise ValidationError({'period': 'פורמט חודש לא תקין — נדרש YYYY-MM'})
        if str(params.get('needs_receipt', '')).lower() in ('1', 'true', 'yes'):
            queryset = queryset.filter(status=TenantCharge.STATUS_CHARGED, receipt__isnull=True)
        queryset = queryset.order_by('-period', '-created_at')[:CHARGES_LIST_CAP]
        return Response(self.get_serializer(queryset, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(self.get_serializer(self.get_object()).data)

    @action(detail=True, methods=['post'])
    def retry(self, request, pk=None):
        """Charge a failed month again, now, on the order's card. {"outcome", "charge"}."""
        charge = self.get_object()
        try:
            outcome, fresh = billing.retry_charge(charge, user=request.user)
        except (BillingDisabled, BillingError) as exc:
            return _billing_error(exc)
        return Response({'outcome': outcome, 'charge': self._read(fresh)})

    @action(detail=True, methods=['post'], url_path='mark-charged')
    def mark_charged(self, request, pk=None):
        charge = self.get_object()
        body = _body(request)
        try:
            fresh = billing.mark_charged(
                charge,
                transaction_id=body.get('transaction_id', ''),
                confirmation_code=body.get('confirmation_code', ''),
                note=body.get('note', ''),
                user=request.user,
            )
        except BillingError as exc:
            return _billing_error(exc)
        return Response(self._read(fresh))

    @action(detail=True, methods=['post'])
    def void(self, request, pk=None):
        charge = self.get_object()
        try:
            fresh = billing.void_charge(charge, reason=_body(request).get('reason', ''), user=request.user)
        except BillingError as exc:
            return _billing_error(exc)
        return Response(self._read(fresh))

    @action(detail=True, methods=['post'], url_path='issue-receipt')
    def issue_receipt(self, request, pk=None):
        """The receipt of a charged month that has none. Issuing twice returns the first one."""
        charge = self.get_object()
        if charge.status != TenantCharge.STATUS_CHARGED:
            return Response({'error': 'קבלה מופקת רק לחיוב שעבר'}, status=status.HTTP_400_BAD_REQUEST)
        if not charge.receipt_id and not billing.issue_receipt_safely(charge.pk):
            return Response(
                {'error': 'הקבלה לא הופקה. החיוב נשאר רשום כחיוב שעבר.', 'charge': self._read(charge)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(self._read(charge))


class BillingStatusView(APIView):
    """Whether rental billing is on, and whether the business its charges are tagged to exists."""

    permission_classes = [IsAuthenticated, IsManagerOrPartner]

    def get(self, request):
        business = billing.rental_business()
        return Response({
            'enabled': billing.billing_enabled(),
            'message': '' if billing.billing_enabled() else DISABLED_MESSAGE,
            'business_name': billing.business_name(),
            'business_found': business is not None,
            'business_id': str(business.id) if business else None,
        })


def _card_error(exc: CardEntryError) -> Response:
    return Response(
        {
            'success': False,
            'error': exc.message,
            'processing': exc.processing,
            'already_done': exc.already_done,
            'disabled': exc.disabled,
        },
        status=exc.status_code,
    )


class PublicCardView(APIView):
    """The tenant's card page. GET: what they are signing up to. POST {"card_details": {...}}: store the card."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'rental_card_view'

    def get_throttles(self):
        # One view, two limits: reading the page, and submitting a card.
        self.throttle_scope = 'rental_card_charge' if self.request.method == 'POST' else 'rental_card_view'
        return super().get_throttles()

    def get(self, request, token: str):
        try:
            link = resolve_link(token)
        except CardEntryError as exc:
            return _card_error(exc)
        return Response(preview_payload(link))

    def post(self, request, token: str):
        try:
            resolve_link(token)
            if not billing.billing_enabled():
                raise CardEntryError(DISABLED_MESSAGE, status_code=503, disabled=True)
        except CardEntryError as exc:
            return _card_error(exc)
        body = _body(request)
        try:
            card = validate_card_details(body.get('card_details') or {})
        except CardValidationError as exc:
            return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = apply_card(token, card)
        except CardEntryError as exc:
            return _card_error(exc)
        return Response(result)


@api_view(['POST'])
@permission_classes([AllowAny])
def cron_charge(request):
    """
    Charge the tenants' standing orders that are due. The courses' cron auth:
    X-Cron-Token, ?token= or a Bearer matching CRON_TOKEN / CRON_SECRET.

    While RENTAL_BILLING_ENABLED is off it answers {"summary": {"disabled": true}}
    and touches nothing. Not in vercel.json: it is scheduled in phase 7.
    """
    from apps.customers.views import _cron_request_authorized

    if not _cron_request_authorized(request):
        return Response({'error': 'unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)
    try:
        limit = int(request.query_params.get('limit') or 40)
    except (TypeError, ValueError):
        limit = 40
    summary = billing.charge_due(limit=limit)
    return Response({'ok': bool(summary.get('ok')), 'summary': summary})
