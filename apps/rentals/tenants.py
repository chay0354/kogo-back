"""Tenants — the merchant records (BusinessCustomer) a tenancy is made with.

A tenant is not a new kind of customer. It is the BusinessCustomer the office
already issues documents to, so a tenant's agreement, invoices and receipts all
land on one card. A tenant created from the tenancy screen is tagged with the
business "סוחרים" and with the tenancy's branch: the tags the document dialog
would give a merchant by hand, and the branch a partner's view is scoped by.
"""
from __future__ import annotations

import logging

from apps.customers.models import BusinessCustomer

logger = logging.getLogger(__name__)

# Seeded by apps/core/migrations/0018_seed_businesses.py.
MERCHANTS_BUSINESS_NAME = 'סוחרים'

# What the tenancy screen may write on a tenant. Tags (business, category,
# branch) are set once, when the tenant is created here, and never rewritten.
TENANT_FIELDS = ('first_name', 'last_name', 'company_number', 'id_number', 'phone', 'email', 'address')


def merchants_business():
    """
    The business every tenant created here is tagged with, or None.

    Looked up by name and never created: the list of businesses is the
    managers' to keep. When the seed is missing the tenant is left untagged,
    with a warning, rather than failing the office's work or inventing a business.
    """
    from apps.core.models import Business

    business = Business.objects.filter(name=MERCHANTS_BUSINESS_NAME).first()
    if business is None:
        logger.warning(
            'Business %r is missing; the new tenant is saved without a business tag',
            MERCHANTS_BUSINESS_NAME,
        )
    return business


def create_tenant(data: dict, branch) -> BusinessCustomer:
    """A new merchant for a tenancy: the given fields, tagged סוחרים and the tenancy's branch."""
    values = {field: data[field] for field in TENANT_FIELDS if field in data}
    return BusinessCustomer.objects.create(**values, business=merchants_business(), branch=branch)


def update_tenant(tenant: BusinessCustomer, data: dict) -> BusinessCustomer:
    """Change only the fields given. The tags stay as they are."""
    changed = [field for field in TENANT_FIELDS if field in data]
    for field in changed:
        setattr(tenant, field, data[field])
    if changed:
        tenant.save(update_fields=[*changed, 'updated_at'])
    return tenant
