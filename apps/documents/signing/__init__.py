"""
חתימה אלקטרונית מאובטחת on kogo's fiscal documents.

הוראה 1 להוראות ניהול פנקסי חשבונות: a document sent by computer is a "מסמך
ממוחשב" only when it is signed with an approved or a secured electronic
signature. The owner chose a secured one (חוק חתימה אלקטרונית, ס' 1, 3; תקנה 8
to the secured-signature regulations): a private key that never leaves a Cloud
KMS HSM, a standard PDF signature (PAdES B-B) and a self-issued certificate.

The parts, each small and replaceable:

- ``backends`` / ``kms``: who holds the key. A local EC key for tests and
  development, Google Cloud KMS in production. Both only ever sign a digest.
- ``certificate``: the public certificate, loaded or built once.
- ``signer``: pyHanko, signing a PDF through a backend and checking the result.
- ``service``: when a document is signed, where its original goes (email,
  paper, held) and the helpers the five email exits call.

Everything is behind DOCUMENT_SIGNING_ENABLED. With it off, nothing here runs
and every mail and download is drawn exactly as before.
"""
from __future__ import annotations

from django.conf import settings


class SigningUnavailable(Exception):
    """The document could not be signed now — no key, no certificate, or the key was out of reach.

    Never raised past the signing service: a document that cannot be signed is
    held and signed later by the sign-pending cron. It never fails a charge.
    """


def enabled() -> bool:
    return bool(getattr(settings, 'DOCUMENT_SIGNING_ENABLED', False))


def consent_enforced() -> bool:
    return bool(getattr(settings, 'COMPUTERIZED_CONSENT_ENFORCED', False))
