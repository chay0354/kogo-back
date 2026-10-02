"""
Limits on the registration form's open endpoints that hold on a serverless host.

`widget/lookup/` and `widget/quote/` answer anyone who sends a parent's
identity number, and what they answer — "this family is with us", "a sibling
discount applies" — is a way to learn who the customers are, one number at a
time. A parent asks a handful of times; a sweep asks thousands. The count is
kept in the database per address and hour, because the DRF throttle's counter
lives in each instance's memory and is forgotten.

A limit never stands in the way of registering: over it, the form is answered
429, treats the answer as "no information", and the registration goes on.
The limiter itself never raises — if it cannot count, it lets the request by.
"""
import hashlib
import hmac
import logging
import random
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

logger = logging.getLogger(__name__)

TOO_MANY_MESSAGE = 'יותר מדי בקשות. נסו שוב בעוד כמה דקות.'
# Kept only as long as it is counted from: buckets two days, answers ninety.
BUCKETS_KEPT = timedelta(days=2)
ATTEMPTS_KEPT = timedelta(days=90)


def _keyed(value: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def over_hourly_limit(scope: str, ip: str, limit: int) -> bool:
    """Count this request; True when the address has now asked more than `limit` times this hour."""
    if not ip:
        return False
    try:
        from apps.customers.identification_models import WidgetRateBucket

        hour = timezone.now().replace(minute=0, second=0, microsecond=0)
        key = dict(scope=scope, key_hash=_keyed(ip), bucket_start=hour)
        try:
            with transaction.atomic():
                bucket, created = WidgetRateBucket.objects.get_or_create(defaults={'count': 1}, **key)
        except IntegrityError:
            bucket, created = WidgetRateBucket.objects.get(**key), False
        if created:
            _tidy_now_and_then()
            return limit < 1
        WidgetRateBucket.objects.filter(pk=bucket.pk).update(count=F('count') + 1)
        return bucket.count + 1 > limit
    except Exception:
        logger.exception('Widget limit %s could not be counted — request let by', scope)
        return False


def too_many(scope: str, request, limit: int):
    """A 429 answer when this address is over the limit, else None."""
    from apps.signatures.capture import client_ip

    if over_hourly_limit(scope, client_ip(request) or '', limit):
        return Response({'error': TOO_MANY_MESSAGE}, status=status.HTTP_429_TOO_MANY_REQUESTS)
    return None


def _tidy_now_and_then() -> None:
    """Drop what is no longer counted from. Run on a few of the requests, never failing one."""
    if random.random() > 0.05:
        return
    try:
        from apps.customers.identification_models import WidgetIdentifyAttempt, WidgetRateBucket

        now = timezone.now()
        WidgetRateBucket.objects.filter(bucket_start__lt=now - BUCKETS_KEPT).delete()
        WidgetIdentifyAttempt.objects.filter(created_at__lt=now - ATTEMPTS_KEPT).delete()
    except Exception:
        logger.exception('Widget limits tidy-up failed (non-fatal)')
