"""The office's endpoints for a payment split between cards (payment_split.py).

    GET  customers/payment-splits/?child_id=…            the child's splits, newest first
    POST customers/payment-splits/preview/                the figures, before anything is made
    POST customers/payment-splits/                        a sign-up paid in parts: a card link per payer
    POST customers/payment-splits/from-standing-order/    split a standing order that bills the whole
    POST customers/payment-splits/<id>/cancel/            close a split nobody has paid into yet

Manager only, like the card links. Nothing here sends a message unless the
body asks (`send`), and nothing charges a card: each payer pays through their
own card link.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.frontend_url import public_frontend_url
from apps.core.permissions import IsManager
from apps.courses.models import Lesson, LessonBundle
from apps.customers.card_link import CardLinkError, quote_standing_order, send_card_link_whatsapp, unit_lessons
from apps.customers.card_link_views import MAX_SPECIAL_MONTHLY_PRICE, _serialize
from apps.customers.models import Child, RecurringPayment
from apps.customers.payment_split import (
    SplitError,
    _whole_quote,
    cancel_waiting_split,
    create_split,
    create_split_from_existing,
    divide,
    divide_like,
    money,
    split_payload,
)
from apps.customers.split_models import MAX_SPLIT_SHARES, PaymentSplit


def _unit(data):
    """(lesson, bundle) from a body, the way the card link reads it, or raise SplitError."""
    lesson = Lesson.objects.select_related('course', 'course__branch').filter(id=data.get('lesson_id')).first() if data.get('lesson_id') else None
    bundle = None
    if data.get('bundle_id'):
        bundle = LessonBundle.objects.prefetch_related('lessons').filter(id=data.get('bundle_id')).first()
        if bundle is None:
            raise SplitError('המסלול לא נמצא')
        members = unit_lessons(lesson=None, bundle=bundle)
        if lesson is None or lesson not in members:
            lesson = members[0] if members else None
    if lesson is None:
        raise SplitError('יש לבחור שיעור או מסלול')
    return lesson, bundle


def _special(data, bundle):
    raw = data.get('special_monthly_price')
    if raw in (None, ''):
        return None
    if bundle is not None:
        raise SplitError('מחיר מיוחד אפשר לקבוע רק לשיעור יחיד')
    try:
        special = money(Decimal(str(raw)))
    except (InvalidOperation, ValueError):
        raise SplitError('מחיר מיוחד לא תקין')
    if special < Decimal('1.00') or special > MAX_SPECIAL_MONTHLY_PRICE:
        raise SplitError('מחיר מיוחד לא תקין')
    return special


def _parts(data, *, given_payers: int | None = None) -> int:
    try:
        parts = int(data.get('parts') or given_payers or 0)
    except (TypeError, ValueError):
        parts = 0
    if parts < 2 or parts > MAX_SPLIT_SHARES:
        raise SplitError(f'אפשר לחלק בין 2 ל־{MAX_SPLIT_SHARES} משלמים')
    return parts


def _amounts(data):
    raw = data.get('amounts')
    if not raw:
        return None
    if not isinstance(raw, list):
        raise SplitError('סכומים לא תקינים')
    return raw


def _links_payload(links, request) -> list[dict]:
    rows = []
    for link in links:
        row = _serialize(link, request)
        try:
            quote = quote_standing_order(link)
            row['quote'] = {
                'first_charge': str(quote['first_charge']), 'monthly_amount': str(quote['monthly_amount']),
                'registration_fee': str(quote['registration_fee']),
                'next_billing_date': quote['next_billing_date'].isoformat(),
            }
        except CardLinkError as exc:
            row['quote_error'] = str(exc)
        rows.append(row)
    return rows


def _error(exc) -> Response:
    return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class PaymentSplitListCreateView(APIView):
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        child_id = request.query_params.get('child_id')
        if not child_id:
            return Response({'error': 'חסר child_id'}, status=status.HTTP_400_BAD_REQUEST)
        splits = PaymentSplit.objects.filter(child_id=child_id).order_by('-created_at')[:20]
        return Response([split_payload(split) for split in splits])

    def post(self, request):
        data = request.data
        child = Child.objects.select_related('family').filter(id=data.get('child_id')).first()
        if child is None:
            return Response({'error': 'ילד לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        payers = data.get('payers') or []
        if not isinstance(payers, list):
            return _error('משלמים לא תקינים')
        try:
            lesson, bundle = _unit(data)
            split, links = create_split(
                child=child, lesson=lesson, bundle=bundle, payers=payers, amounts=_amounts(data),
                special=_special(data, bundle), include_fee=bool(data.get('include_registration_fee', True)),
                created_by=request.user,
            )
        except SplitError as exc:
            return _error(exc)
        payload = {'split': split_payload(split), 'links': _links_payload(links, request)}
        if data.get('send'):
            base = public_frontend_url(request)
            payload['whatsapp'] = [send_card_link_whatsapp(link, base) for link in links]
        return Response(payload, status=status.HTTP_201_CREATED)


def _family_parents(child) -> list[dict]:
    """The parents the office can pick as payers."""
    if child is None or not child.family_id:
        return []
    return [
        {
            'id': str(parent.id), 'name': f'{parent.first_name} {parent.last_name}'.strip(),
            'phone': parent.phone, 'email': parent.email, 'is_primary': parent.is_primary,
        }
        for parent in child.family.parents.order_by('-is_primary', 'created_at')
    ]


class PaymentSplitPreviewView(APIView):
    """The whole and its shares, as they would be made — nothing is written."""
    permission_classes = [IsAuthenticated, IsManager]

    def post(self, request):
        data = request.data
        try:
            amounts = _amounts(data)
            recurring_id = data.get('recurring_payment_id')
            if recurring_id:
                recurring = RecurringPayment.objects.filter(id=recurring_id).first()
                if recurring is None:
                    return Response({'error': 'הוראת הקבע לא נמצאה'}, status=status.HTTP_404_NOT_FOUND)
                parts = _parts(data)
                monthly = divide(recurring.amount, parts, amounts)
                from apps.customers.payment_split import _primary_payer

                first = _primary_payer(recurring.child, recurring)
                return Response({
                    'monthly_total': str(money(recurring.amount)),
                    'registration_fee_total': '0.00',
                    'shares': [{'monthly_amount': str(m), 'registration_fee': '0.00'} for m in monthly],
                    'parents': _family_parents(recurring.child),
                    'first_payer': {'name': first['name'], 'phone': first['phone']},
                    'next_billing_date': recurring.next_billing_date.isoformat() if recurring.next_billing_date else None,
                })
            child = Child.objects.select_related('family').filter(id=data.get('child_id')).first()
            if child is None:
                return Response({'error': 'ילד לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
            lesson, bundle = _unit(data)
            parts = _parts(data)
            whole = _whole_quote(
                child=child, lesson=lesson, bundle=bundle, special=_special(data, bundle),
                include_fee=bool(data.get('include_registration_fee', True)),
            )
            monthly = divide(whole['monthly_amount'], parts, amounts)
            fees = divide_like(whole['registration_fee'], monthly)
        except (SplitError, CardLinkError) as exc:
            return _error(exc)
        return Response({
            'monthly_total': str(money(whole['monthly_amount'])),
            'registration_fee_total': str(money(whole['registration_fee'])),
            'discounts': [
                {'name': item.name, 'reason': item.reason, 'value': str(money(item.value))}
                for item in whole['discounts']
            ],
            'shares': [{'monthly_amount': str(m), 'registration_fee': str(f)} for m, f in zip(monthly, fees)],
            'parents': _family_parents(child),
        })


class PaymentSplitFromStandingOrderView(APIView):
    permission_classes = [IsAuthenticated, IsManager]

    def post(self, request):
        data = request.data
        recurring = (
            RecurringPayment.objects.select_related('child', 'child__family', 'initial_payment', 'initial_payment__lesson',
                                                   'initial_payment__lesson__course', 'initial_payment__bundle')
            .filter(id=data.get('recurring_payment_id')).first()
        )
        if recurring is None:
            return Response({'error': 'הוראת הקבע לא נמצאה'}, status=status.HTTP_404_NOT_FOUND)
        payers = data.get('payers') or []
        if not isinstance(payers, list):
            return _error('משלמים לא תקינים')
        try:
            split, links = create_split_from_existing(
                recurring=recurring, payers=payers, amounts=_amounts(data), created_by=request.user,
            )
        except SplitError as exc:
            return _error(exc)
        payload = {'split': split_payload(split), 'links': _links_payload(links, request)}
        if data.get('send'):
            base = public_frontend_url(request)
            payload['whatsapp'] = [send_card_link_whatsapp(link, base) for link in links]
        return Response(payload, status=status.HTTP_201_CREATED)


class PaymentSplitCancelView(APIView):
    permission_classes = [IsAuthenticated, IsManager]

    def post(self, request, split_id):
        split = PaymentSplit.objects.filter(id=split_id).first()
        if split is None:
            return Response({'error': 'החלוקה לא נמצאה'}, status=status.HTTP_404_NOT_FOUND)
        try:
            cancel_waiting_split(split)
        except SplitError as exc:
            return _error(exc)
        return Response(split_payload(split))
