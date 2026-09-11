"""The public link a tenant enters their card through — `<crm>/rc/<token>`.

The token is the whole credential: 12 random base-62 characters (about 71
bits), unique in the table, and the page is throttled on top of that. A link is
good for 14 days from when it was issued and is used once. Issuing a new one
takes the previous one out of play; the database holds one live link per order.

Links open only for an order waiting for a card or one whose charge failed.
Sending the link (WhatsApp, e-mail) is not done here: the office copies the URL,
and the signing page (phase 3) leads the tenant straight to it.
"""
from __future__ import annotations

import secrets
import string
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.frontend_url import public_frontend_url
from apps.rental_billing.errors import BillingError
from apps.rental_billing.models import TenantCardLink, TenantStandingOrder

LINK_MAX_AGE = timedelta(days=14)
# A submit that has not come back in this long never will (the function was
# killed mid-call). Money may have moved, so it goes to review, never back to pending.
PROCESSING_STALE_AFTER = timedelta(seconds=90)
PUBLIC_PATH = 'rc'
TOKEN_LENGTH = 12
_ALPHABET = string.ascii_letters + string.digits

LINKABLE_STATUSES = (TenantStandingOrder.STATUS_PENDING_CARD, TenantStandingOrder.STATUS_FAILED)


def new_token() -> str:
    while True:
        token = ''.join(secrets.choice(_ALPHABET) for _ in range(TOKEN_LENGTH))
        if not TenantCardLink.objects.filter(token=token).exists():
            return token


def public_url(link: TenantCardLink, base: str | None = None) -> str:
    return f'{(base or public_frontend_url()).rstrip("/")}/{PUBLIC_PATH}/{link.token}'


def expires_at(link: TenantCardLink):
    return link.created_at + LINK_MAX_AGE


def is_expired(link: TenantCardLink, now=None) -> bool:
    return (now or timezone.now()) > expires_at(link)


def is_in_flight(link: TenantCardLink, now=None) -> bool:
    """A submit is running on this link right now."""
    return (
        link.status == TenantCardLink.STATUS_PROCESSING
        and link.charge_started_at is not None
        and (now or timezone.now()) - link.charge_started_at < PROCESSING_STALE_AFTER
    )


def _user_or_none(user):
    return user if getattr(user, 'is_authenticated', False) else None


def _retire(link: TenantCardLink) -> None:
    """Out of play: a waiting link is cancelled; one that never came back from a submit goes to review."""
    if link.status == TenantCardLink.STATUS_PROCESSING:
        link.status = TenantCardLink.STATUS_REVIEW
        link.review_reason = 'stale_processing'
    else:
        link.status = TenantCardLink.STATUS_CANCELLED
    link.save(update_fields=['status', 'review_reason', 'updated_at'])


def cancel_live_links(order) -> int:
    """Cancel the order's waiting link. One mid-submit is left alone: the submit re-checks the order before it stores anything."""
    return TenantCardLink.objects.filter(
        standing_order_id=order.pk, status=TenantCardLink.STATUS_PENDING,
    ).update(status=TenantCardLink.STATUS_CANCELLED, updated_at=timezone.now())


def ensure_card_link(order, user=None) -> TenantCardLink:
    """
    The order's live link, or a new one when it has none (or it expired).

    Runs inside the caller's transaction with the order row locked: a decline
    on the monthly run opens a link for the tenant this way.
    """
    now = timezone.now()
    live = (
        TenantCardLink.objects.select_for_update()
        .filter(standing_order_id=order.pk, status__in=TenantCardLink.LIVE_STATUSES)
        .first()
    )
    if live is not None and not is_expired(live, now):
        return live
    if live is not None:
        if is_in_flight(live, now):
            return live
        _retire(live)
    return TenantCardLink.objects.create(standing_order_id=order.pk, token=new_token(), created_by=_user_or_none(user))


def rotate_card_link(order, user=None) -> TenantCardLink:
    """A new URL for the order; the previous one stops working. Refused while a submit is running on it."""
    with transaction.atomic():
        locked = TenantStandingOrder.objects.select_for_update().get(pk=order.pk)
        if locked.status not in LINKABLE_STATUSES:
            raise BillingError('אפשר לשלוח קישור לכרטיס רק להוראת קבע שממתינה לכרטיס או שהחיוב בה נכשל')
        live = (
            TenantCardLink.objects.select_for_update()
            .filter(standing_order=locked, status__in=TenantCardLink.LIVE_STATUSES)
            .first()
        )
        if live is not None:
            if is_in_flight(live):
                raise BillingError('השוכר מזין כרטיס ממש עכשיו — נסו שוב בעוד דקה', status_code=409)
            _retire(live)
        return TenantCardLink.objects.create(standing_order=locked, token=new_token(), created_by=_user_or_none(user))
