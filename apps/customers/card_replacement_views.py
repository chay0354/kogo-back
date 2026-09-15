"""
Card replacement endpoints.

CRM (manager or partner): quote a family, then replace the card.
Public (the parent, no auth, throttled): preview by signed token, submit a card.

Both sides call the same `card_replacement` functions, so the office and the
parent can never end up with two different ideas of what is owed.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.card_validation import CardValidationError
from apps.core.permissions import IsManagerOrPartner
from apps.customers.card_replacement import (
    CardReplacementError,
    children_without_standing_order,
    family_public_url,
    public_preview,
    quote,
    replace_card,
    resolve_family_token,
)
from apps.customers.models import Child, Family


def _family_or_404(family_id: str):
    return Family.objects.filter(id=family_id).first()


class FamilyCardQuoteView(APIView):
    """GET the standing orders and the arrears before anyone types a card."""
    permission_classes = [IsAuthenticated, IsManagerOrPartner]

    def get(self, request, family_id: str):
        family = _family_or_404(family_id)
        if family is None:
            return Response({'error': 'משפחה לא נמצאה'}, status=status.HTTP_404_NOT_FOUND)
        payload = quote(family)
        payload['link'] = family_public_url(family)
        return Response(payload)


class ChildrenWithoutStandingOrderView(APIView):
    """
    Who is enrolled, paying, and has no standing order behind them.

    GET /api/v1/customers/children-without-standing-order/?branch_id=

    Reads only. The office decides what to do with each row — usually replacing
    the card, which is what creates the standing order that was never made.
    """
    permission_classes = [IsAuthenticated, IsManagerOrPartner]

    def get(self, request):
        rows = children_without_standing_order(
            branch_id=request.query_params.get('branch_id') or None,
        )
        return Response({'count': len(rows), 'results': rows})


class FamilyCardReplaceView(APIView):
    """POST a card. Saves it on every standing order, then collects the arrears."""
    permission_classes = [IsAuthenticated, IsManagerOrPartner]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'card_replace'

    def post(self, request, family_id: str):
        family = _family_or_404(family_id)
        if family is None:
            return Response({'error': 'משפחה לא נמצאה'}, status=status.HTTP_404_NOT_FOUND)
        try:
            result = replace_card(
                family,
                request.data.get('card_details') or {},
                actor=request.user,
                source='crm',
                charge=bool(request.data.get('charge', True)),
            )
        except (CardValidationError, CardReplacementError) as exc:
            return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)


class ChildFamilyCardQuoteView(APIView):
    """The same quote reached from a child, because that is where the office starts."""
    permission_classes = [IsAuthenticated, IsManagerOrPartner]

    def get(self, request, child_id: str):
        child = Child.objects.select_related('family').filter(id=child_id).first()
        if child is None or child.family is None:
            return Response({'error': 'ילד לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        payload = quote(child.family)
        payload['link'] = family_public_url(child.family)
        return Response(payload)


class PublicCardReplacePreviewView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'card_replace_view'

    def get(self, request, token: str):
        try:
            family = resolve_family_token(token)
        except CardReplacementError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(public_preview(family))


class PublicCardReplaceApplyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'card_replace'

    def post(self, request, token: str):
        try:
            family = resolve_family_token(token)
        except CardReplacementError as exc:
            return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = replace_card(
                family,
                request.data.get('card_details') or {},
                actor=None,
                source='parent_link',
            )
        except (CardValidationError, CardReplacementError) as exc:
            return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        # The parent is told what happened to their money, not which orders exist.
        return Response({
            'success': True,
            'charged_total': result['charged_total'],
            'standing_orders_updated': result['standing_orders_updated'],
            'declined': len(result['declined']),
        })
