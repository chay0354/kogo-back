import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsManager
from apps.core.tranzila_service import TranzilaService

logger = logging.getLogger(__name__)

MAX_CHARGE_AMOUNT = Decimal('5')


class CreditCardChargeView(APIView):
    """
    Charge a real credit card directly, via Tranzila's PRODUCTION terminal
    (TRANZILA_PROD_* settings), capped at MAX_CHARGE_AMOUNT.

    Manager-only. Does not persist anything (no Payment/TranzilaTransaction row) —
    this is a standalone charge, not part of the enrollment/invoice flow.

    POST /api/v1/core/credit-cards/charge/
    Body: {
        "card_holder_name": "...",   # informational only, not sent to Tranzila
        "card_number": "...",
        "expiry_month": 12,
        "expiry_year": 2026,
        "cvv": "123",
        "card_holder_id": "012345678",
        "amount": "5.00",
        "notes": "..."                # informational only, not sent to Tranzila
    }
    """
    permission_classes = [IsAuthenticated, IsManager]

    def post(self, request):
        data = request.data or {}

        try:
            amount = Decimal(str(data.get('amount', '')))
        except (InvalidOperation, TypeError):
            return Response({'error': 'סכום לא תקין'}, status=status.HTTP_400_BAD_REQUEST)

        if amount <= 0 or amount > MAX_CHARGE_AMOUNT:
            return Response(
                {'error': f'הסכום חייב להיות בין 0 ל-{MAX_CHARGE_AMOUNT} ש"ח'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        card_number = str(data.get('card_number', '')).replace(' ', '')
        cvv = str(data.get('cvv', ''))
        card_holder_id = str(data.get('card_holder_id', ''))

        try:
            expiry_month = int(data.get('expiry_month'))
            expiry_year = int(data.get('expiry_year'))
        except (TypeError, ValueError):
            return Response({'error': 'תוקף כרטיס לא תקין'}, status=status.HTTP_400_BAD_REQUEST)

        if not card_number or not cvv or not card_holder_id:
            return Response({'error': 'פרטי כרטיס נדרשים'}, status=status.HTTP_400_BAD_REQUEST)

        logger.info(
            "Credit card charge attempt via prod terminal by user_id=%s amount=%s",
            getattr(request.user, 'id', None),
            amount,
        )

        tranzila = TranzilaService(
            terminal=settings.TRANZILA_PROD_TERMINAL,
            token_terminal=settings.TRANZILA_PROD_TOKEN_TERMINAL,
            supplier=settings.TRANZILA_PROD_SUPPLIER,
            public_key=settings.TRANZILA_PROD_PUBLIC_KEY,
            secret_key=settings.TRANZILA_PROD_SECRET_KEY,
        )

        result = tranzila.charge_with_card(
            card_number=card_number,
            expiry_month=expiry_month,
            expiry_year=expiry_year,
            cvv=cvv,
            card_holder_id=card_holder_id,
            amount=amount,
            description='חיוב כרטיס אשראי - עמוד ניהול',
        )

        status_code = status.HTTP_200_OK if result.get('success') else status.HTTP_400_BAD_REQUEST
        return Response(result, status=status_code)
