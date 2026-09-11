"""Consent to receive tax documents by email — סעיף 18ב להוראות ניהול פנקסי חשבונות.

Sending a חשבונית מס, קבלה or הודעת זיכוי by computer is permitted only to a
customer who consented to receive computerized documents and has not withdrawn
that consent (סעיף 18ב(ג)), and only after פקיד השומה was notified by registered
mail before the first such document went out (סעיף 18ב(ב)).

Consent was never recorded before this module existed, so `check_consent` only
reports — it does not withhold a document a customer is waiting for. Once the
recorded consent covers the active families, flip the call sites to refuse.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

CONSENT_SOURCE_WIDGET = 'widget'
CONSENT_SOURCE_CRM = 'crm'
CONSENT_SOURCE_WEBSITE = 'website'


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
