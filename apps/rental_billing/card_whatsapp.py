"""Sending a tenant the link to enter a card, on WhatsApp.

Phase 4 left this open in so many words: a declined charge opens a card link
(billing.record_result → links.ensure_card_link) and nobody sent it. This is
the sender, on the `rental-card-update` automation.

Two ways in, the same two the courses' card-update send has:

* the monthly run, right after a decline (billing.charge_due). It is wrapped
  there in try/except and never fails the run: the money question is already
  settled by then, and a message that did not go out must not undo it;
* the office, on one order (the send-card-link endpoint).

The link itself is never made here. ensure_card_link is idempotent and the
decline has already run it, so the send picks up the URL the tenant is meant to
have; with no live link there is nothing to send and the answer says so. That
also means sending twice sends the same URL twice, rather than quietly
invalidating one the tenant already opened.

Never raises, like every other send: {'sent': False, 'reason': ...} instead.
"""
from __future__ import annotations

import logging

from apps.core.frontend_url import public_frontend_url
from apps.core.manychat_service import SUPPORT_PHONE, ManyChatService
from apps.rental_billing import billing
from apps.rental_billing.links import public_url
from apps.rental_billing.models import TenantCardLink
from apps.rentals.tenant_whatsapp import build_tenant_whatsapp_context

logger = logging.getLogger(__name__)

NO_LINK = 'no_live_link'
NO_PHONE = 'no_tenant_phone'


def live_card_link(order) -> TenantCardLink | None:
    """The URL the tenant is meant to be holding, or None when the order has none out."""
    return (
        TenantCardLink.objects
        .filter(standing_order_id=order.pk, status__in=TenantCardLink.LIVE_STATUSES)
        .order_by('-created_at')
        .first()
    )


def send_card_link_whatsapp(order, request=None, link=None) -> dict:
    """
    The order's live card link to the tenant on WhatsApp.

    The amount named is the order's own monthly total, not the declined
    charge's: what the tenant is being asked to put a card behind is the
    standing order, and a month the office later voids would otherwise leave
    the message quoting a sum nobody owes.
    """
    link = link or live_card_link(order)
    if link is None:
        return {'sent': False, 'reason': NO_LINK}
    ctx = build_tenant_whatsapp_context(order.tenant, branch=order.branch)
    if ctx is None:
        return {'sent': False, 'reason': NO_PHONE}

    _net, _vat, total = billing.split_amount(order.amount_before_vat)
    lookup_names = ctx.pop('lookup_names', None)
    extra_fields = {
        # The same field names the courses' card links write, so an owner who
        # maps the rental template to them has nothing new to create.
        'kogo_card_update_url': public_url(link, public_frontend_url(request)),
        'kogo_card_update_token': link.token,
        'kogo_amount': str(billing.shekels(total)),
        'kogo_support_phone': SUPPORT_PHONE,
    }
    result = ManyChatService().notify_registration(
        kind=ManyChatService.REGISTRATION_KIND_RENTAL_CARD_UPDATE,
        lookup_names=lookup_names,
        extra_fields=extra_fields,
        **ctx,
    )
    if result.get('sent'):
        logger.info(
            'Rental standing order %s: card link sent on WhatsApp via %s', order.pk, result.get('method'),
        )
    else:
        logger.warning(
            'Rental standing order %s: card link NOT sent on WhatsApp: %s', order.pk, result.get('reason'),
        )
    return result


def send_card_link_after_decline(order) -> None:
    """
    The decline's own send, from the monthly run. Swallows everything.

    The charge is already recorded and the link already opened when this runs.
    Nothing it can do may change either, so a ManyChat outage, a tenant with no
    phone or a missing automation is logged and the run carries on.
    """
    try:
        send_card_link_whatsapp(order)
    except Exception:
        logger.exception(
            'Rental standing order %s: the card-link WhatsApp failed after a decline '
            '(the charge and the link stand)', order.pk,
        )
