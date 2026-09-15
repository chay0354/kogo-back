"""Sending a tenant the link to read and sign their contract, on WhatsApp.

The office has always been able to copy the URL or open its own WhatsApp on it
(phase 3). This is the automatic send: the same link, through ManyChat, on the
`rental-contract` automation.

Nothing here makes, rotates or withdraws a link, and nothing here touches the
contract row. A contract is frozen once issued and its link fields are written
only by apps/rentals/signing.py; a send that also wrote to the row would have
to be undone by hand when the message failed. So the office asks for a link
first (issue_signing_link) and sends it second, and sending twice sends the
same URL twice — which is what "re-send" should mean.

Never raises. Like every other send in the system it answers
{'sent': False, 'reason': ...} so the caller can say why, and a message that
did not go out leaves the contract exactly as it was.
"""
from __future__ import annotations

import logging

from apps.core.manychat_service import SUPPORT_PHONE, ManyChatService
from apps.rentals.models import RentalContract
from apps.rentals.signing import link_is_live, signing_url
from apps.rentals.tenant_whatsapp import build_tenant_whatsapp_context

logger = logging.getLogger(__name__)

NO_LINK = 'no_live_link'
NO_PHONE = 'no_tenant_phone'


def contract_amount_label(contract) -> str:
    """What the contract says the tenant pays each month, VAT included, off its frozen terms."""
    terms = contract.terms if isinstance(contract.terms, dict) else {}
    return str(terms.get('monthly_total') or '')


def send_contract_link_whatsapp(contract: RentalContract, request=None) -> dict:
    """
    The signing link to the tenant on WhatsApp. The contract must have a live one.

    The link is read off the contract, but the tenant's phone is read off the
    live BusinessCustomer: the office corrects a wrong number by editing the
    tenant, and re-sending must then reach the corrected one. (What the message
    is *about* — the amount — still comes from the frozen terms, because that
    is what the tenant is being asked to sign.)
    """
    if not link_is_live(contract):
        return {'sent': False, 'reason': NO_LINK}
    tenancy = contract.tenancy
    ctx = build_tenant_whatsapp_context(tenancy.tenant, branch=tenancy.branch)
    if ctx is None:
        return {'sent': False, 'reason': NO_PHONE}

    lookup_names = ctx.pop('lookup_names', None)
    extra_fields = {
        # The one User Field these sends add to the ones ManyChat already holds.
        # A field that does not exist in ManyChat is dropped by set_custom_fields,
        # so the template would render an empty link — the owner creates it once.
        'kogo_rental_sign_url': signing_url(contract, request),
        'kogo_amount': contract_amount_label(contract),
        'kogo_support_phone': SUPPORT_PHONE,
    }
    result = ManyChatService().notify_registration(
        kind=ManyChatService.REGISTRATION_KIND_RENTAL_CONTRACT,
        lookup_names=lookup_names,
        extra_fields=extra_fields,
        **ctx,
    )
    if result.get('sent'):
        logger.info(
            'Rental contract %s (tenancy %s, version %s): signing link sent on WhatsApp via %s',
            contract.pk, contract.tenancy_id, contract.version, result.get('method'),
        )
    else:
        logger.warning(
            'Rental contract %s: signing link NOT sent on WhatsApp: %s',
            contract.pk, result.get('reason'),
        )
    return result
