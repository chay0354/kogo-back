"""Consent to receive tax documents by email — סעיף 18ב להוראות ניהול פנקסי חשבונות.

Sending a חשבונית מס, קבלה or הודעת זיכוי by computer is permitted only to a
customer who consented to receive computerized documents and has not withdrawn
that consent (סעיף 18ב(ג)), and only after פקיד השומה was notified by registered
mail before the first such document went out (סעיף 18ב(ב)).

Consent was never recorded before this module existed, so `check_consent` only
reports — it does not withhold a document a customer is waiting for. Once the
recorded consent covers the active families, flip the call sites to refuse.

The registration widget records consent when the parent ticks its box, and the
office records or withdraws it from the family card. Both go through
`record_consent` / `revoke_consent`, so the record has one shape whoever took it.
"""
from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

CONSENT_SOURCE_WIDGET = 'widget'
CONSENT_SOURCE_CRM = 'crm'
CONSENT_SOURCE_WEBSITE = 'website'
CONSENT_SOURCES = (CONSENT_SOURCE_WIDGET, CONSENT_SOURCE_CRM, CONSENT_SOURCE_WEBSITE)

_CONSENT_FIELDS = (
    'computerized_docs_consent_at',
    'computerized_docs_consent_source',
    'computerized_docs_consent_revoked_at',
)


def record_consent(family, source: str, *, when=None):
    """Record that the family consented at `source`, and return the family.

    A family that already consents is left as it is: the consent on record keeps
    its moment and where it was given, so a parent ticking the box again on a
    later registration does not move it. A family without consent — never given,
    or withdrawn — consents from `when` (default now), and the withdrawal is
    cleared.
    """
    if source not in CONSENT_SOURCES:
        raise ValueError(f'Unknown consent source: {source!r}')
    if family.accepts_computerized_documents:
        return family
    family.computerized_docs_consent_at = when or timezone.now()
    family.computerized_docs_consent_source = source
    family.computerized_docs_consent_revoked_at = None
    _save_consent(family)
    return family


def revoke_consent(family, *, when=None):
    """Withdraw the family's consent from `when` (default now), and return the family.

    Only a standing consent is withdrawn. A family with none to withdraw is left
    as it is, so an earlier withdrawal keeps its date.
    """
    if not family.accepts_computerized_documents:
        return family
    family.computerized_docs_consent_revoked_at = when or timezone.now()
    _save_consent(family)
    return family


def _save_consent(family):
    # The consent columns only: the widget holds a family it is updating in the
    # same request, and the office's click must not write back a stale card.
    fields = list(_CONSENT_FIELDS)
    if any(field.name == 'updated_at' for field in family._meta.concrete_fields):
        fields.append('updated_at')
    family.save(update_fields=fields)


def check_consent(family, document_ref: str) -> bool:
    """Return whether the family consented, logging the gap when they have not."""
    if family is None:
        return False
    if family.accepts_computerized_documents:
        return True

    logger.warning(
        'Sending computerized document %s to family %s with no recorded consent '
        '(סעיף 18ב(ג)) — record it on the family once collected',
        document_ref,
        family.pk,
    )
    return False
