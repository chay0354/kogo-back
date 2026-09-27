"""
Public endpoints of the hosted-page course checkout (apps/customers/course_checkout.py).

start  — the widget asks for Tranzila's page for its pending payments; with
         COURSE_HOSTED_PAGE_ENABLED off it is told to keep its own card form.
notify — Tranzila's notify. Public and unsigned; nothing is believed until the
         terminal's report says so. Not throttled: Tranzila retries.
status — the widget's poll while the parent is on the page.
"""
import logging

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.customers import course_checkout

logger = logging.getLogger(__name__)


class _Public(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]


class CourseCheckoutStartView(_Public):
    """
    POST /api/v1/customers/widget/checkout/start/ {"payment_ids": ["uuid", ...]}
    → {"checkout_id", "url", "amount"} | {"use_card_form": true}
    """
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'course_checkout_start'

    def post(self, request):
        payment_ids = request.data.get('payment_ids') or []
        if request.data.get('payment_id'):
            payment_ids = [request.data.get('payment_id')]
        if not isinstance(payment_ids, list):
            return Response({'error': 'payment_ids חייב להיות רשימה'}, status=status.HTTP_400_BAD_REQUEST)
        if not course_checkout.hosted_checkout_enabled(payment_ids):
            return Response({'use_card_form': True})
        try:
            checkout, url = course_checkout.start_checkout(payment_ids)
        except course_checkout.CheckoutError as exc:
            return Response({'error': str(exc)}, status=exc.status_code)
        return Response({'checkout_id': str(checkout.id), 'url': url, 'amount': str(checkout.amount)})


@method_decorator(csrf_exempt, name='dispatch')
class CourseCheckoutNotifyView(_Public):
    """POST /api/v1/customers/widget/checkout/notify/ — Tranzila's notify. Always 200 with a verdict."""

    def post(self, request):
        result = course_checkout.handle_notify(request.data)
        return Response(result)


class CourseCheckoutStatusView(_Public):
    """
    GET /api/v1/customers/widget/checkout/<id>/[?index=&code=]
    `index` / `code` are what the result page inside Tranzila's frame saw, for
    when the notify did not come; the verdict is still the report's.
    """
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'course_checkout_status'

    def get(self, request, checkout_id):
        index = str(request.query_params.get('index') or '').strip()
        payload = course_checkout.checkout_status(
            checkout_id,
            index=index if index.isdigit() else '',
            confirmation_code=str(request.query_params.get('code') or '').strip(),
        )
        if payload is None:
            return Response({'error': 'לא נמצא'}, status=status.HTTP_404_NOT_FOUND)
        return Response(payload)
