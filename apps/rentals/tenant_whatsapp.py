"""The WhatsApp fields a message to a studio tenant carries.

apps/core/enrollment_whatsapp.py does this for a parent and a lesson. A tenant
is neither: they are a BusinessCustomer (a merchant), and what they are written
to about is an agreement, not a course. The two sends built on this — the
contract to sign (apps/rentals/contract_whatsapp.py) and the card link after a
declined charge (apps/rental_billing/card_whatsapp.py) — go through the same
ManyChatService.notify_registration as every other send, so they get the same
subscriber lookup, the same "write the fields, wait, then fire the template"
order, and the same 24-hour rule: free text only reaches a contact inside the
window, so a real send needs an approved template behind an automation.

notify_registration's field names were built for parents, and ManyChat holds
them as User Fields the owner's templates are mapped to. Rather than invent a
second set, a tenant's message fills the ones that still mean something:

    kogo_parent_name   the tenant — the name the message opens with
    kogo_child_name    the tenant again, so a template mapped to it is never
                       empty (ManyChat renders an unset variable as nothing,
                       and an approved template with a blank variable reads
                       like a broken message)
    kogo_course_name   'שכירות סטודיו' — what this message is about
    kogo_branch_name   the branch whose studio is rented
    kogo_location      that branch's address, else its name

The day and time fields are left out: an agreement is not a weekly hour, and
set_custom_fields skips empty values anyway. Everything specific to a tenant
send — the link, the amount — is passed as extra_fields by the caller.
"""
from __future__ import annotations

COURSE_LABEL = 'שכירות סטודיו'


def build_tenant_whatsapp_context(tenant, *, branch=None) -> dict | None:
    """
    notify_registration's kwargs for one tenant, or None when there is nobody to write to.

    None means exactly one thing: no phone on the tenant's record. The office
    fixes that by editing the tenant, which is why the caller reads the live
    BusinessCustomer and not the phone frozen into a contract's terms — a
    number corrected after the contract was issued is the one that is used.
    """
    if tenant is None:
        return None
    phone = (getattr(tenant, 'phone', '') or '').strip()
    if not phone:
        return None

    name = (tenant.full_name or '').strip()
    branch_name = (getattr(branch, 'name', '') or '').strip() if branch is not None else ''
    address = (getattr(branch, 'address', '') or '').strip() if branch is not None else ''

    # The names ManyChat is searched by when the phone finds nobody. A merchant
    # is often saved there under the business name, sometimes under a person's.
    lookup_names: list[str] = []
    for value in (name, tenant.first_name, tenant.last_name):
        text = (value or '').strip()
        if text and text not in lookup_names:
            lookup_names.append(text)
        for word in text.split():
            if len(word) >= 2 and word not in lookup_names:
                lookup_names.append(word)

    return {
        'phone': phone,
        'parent_name': name,
        'lookup_names': lookup_names,
        'child_name': name,
        'course_name': COURSE_LABEL,
        'branch_name': branch_name,
        'location': address or branch_name,
        'day_name': '',
        'start_time': '',
        'end_time': '',
    }
