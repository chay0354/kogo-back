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
    resolve_card_update_intent,
)


class CardUpdatePreviewView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, token: str):
        try:
            intent = resolve_card_update_intent(token)
        except CardUpdateError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            preview_payload(intent.recurring, already_done=intent.already_done, intent=intent)
        )


class CardUpdateChargeView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, token: str):
        try:
            intent = resolve_card_update_intent(token)
        except CardUpdateError as exc:
            return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if intent.already_done:
            return Response({
                'success': True,
                'already_done': True,
                'charged': False,
                'mode': intent.mode,
                'message': 'הכרטיס כבר עודכן.',
            })

        try:
            card = validate_card_details(request.data.get('card_details') or {})
        except CardValidationError as exc:
            return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = apply_new_card(intent.recurring, card, intent=intent)
        except CardUpdateError as exc:
            if exc.already_done:
                return Response({
                    'success': True,
                    'already_done': True,
                    'charged': False,
                    'mode': intent.mode,
                    'message': 'הכרטיס כבר עודכן.',
                })
            return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)
