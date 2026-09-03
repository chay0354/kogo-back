"""Public (no-auth) endpoints for the standing-order card-update link."""
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from apps.core.card_validation import CardValidationError, validate_card_details
from apps.customers.card_update import (
    CardUpdateError,
    apply_new_card,
    preview_payload,
    resolve_card_update_token,
)


class CardUpdatePreviewView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, token: str):
        try:
            recurring, already_done = resolve_card_update_token(token)
        except CardUpdateError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(preview_payload(recurring, already_done=already_done))


class CardUpdateChargeView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, token: str):
        try:
            recurring, already_done = resolve_card_update_token(token)
        except CardUpdateError as exc:
            return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if already_done:
            return Response({
                'success': True,
                'already_done': True,
                'charged': False,
            })

        try:
            card = validate_card_details(request.data.get('card_details') or {})
        except CardValidationError as exc:
            return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = apply_new_card(recurring, card)
        except CardUpdateError as exc:
            return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)
