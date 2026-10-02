"""
The registration form asks whether a parent is already with us.

  GET  /api/v1/customers/widget/identify/  — is identification on, and the
       ticket the form must bring back when it asks.
  POST /api/v1/customers/widget/identify/  — an identity number and a phone
       (or the token of a similar number just offered); the answer is "known"
       with hidden details, "near" with one digit, or "unknown".

All of the deciding is in apps/customers/widget_identification.py.
"""
import time

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.customers import widget_identification
from apps.customers.widget_identification import UNKNOWN, form_ticket, identify, is_enabled
from apps.customers.widget_limits import over_hourly_limit, request_ip


# Per address and hour, on top of the limits per identity number and per device.
# One address can be many parents on one mobile network.
IDENTIFY_HOURLY_LIMIT = 120


class WidgetIdentifyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'widget_identify'

    def get(self, request):
        enabled = is_enabled()
        return self._never_stored(Response({'enabled': enabled, 'ticket': form_ticket() if enabled else ''}))

    def post(self, request):
        data = request.data if hasattr(request.data, 'get') else {}
        ip = request_ip(request)
        # Over the hourly count the answer is the one everybody refused gets — and it takes as long.
        if over_hourly_limit('identify', ip, IDENTIFY_HOURLY_LIMIT):
            time.sleep(widget_identification.MIN_ANSWER_SECONDS)
            return self._never_stored(Response(dict(UNKNOWN)))
        return self._never_stored(Response(identify(data, ip=ip)))

    @staticmethod
    def _never_stored(response):
        response['Cache-Control'] = 'no-store'
        return response
