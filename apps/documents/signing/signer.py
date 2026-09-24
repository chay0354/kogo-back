"""
Signing a PDF: PAdES baseline B-B, an invisible signature field, then a check.

pyHanko builds the CMS signature and the PDF's incremental update; the key's
part is one call to the backend with the digest of the signed attributes —
the custom-signer pattern pyHanko documents for Cloud KMS (a ``Signer`` whose
``async_sign_raw`` hashes and calls out). The field is invisible: the
document's face already says "חתום בחתימה אלקטרונית מאובטחת" in its small
print, and a stamp would move the layout the tests and the customers know.

Every signed file is validated before it is returned — the signature must be
intact, cryptographically valid, cover the whole file, and chain to our own
certificate. A file that fails is never stored: a broken signature on an
original is worse than a document held for a few minutes.
"""
from __future__ import annotations

import hashlib
import io
import logging

from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign import signers
from pyhanko.sign.fields import SigSeedSubFilter
from pyhanko.sign.general import SigningError
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko.sign.validation.status import SignatureCoverageLevel
from pyhanko_certvalidator import ValidationContext
from pyhanko_certvalidator.registry import SimpleCertificateStore

from apps.documents.issuer import ISSUER_EMAIL, ISSUER_NAME
from apps.documents.signing import SigningUnavailable
from apps.documents.signing.backends import SigningBackend, get_backend

logger = logging.getLogger(__name__)

FIELD_NAME = 'KogoSignature'
DEFAULT_REASON = 'מקור של מסמך ממוחשב, חתום בחתימה אלקטרונית מאובטחת'
DEFAULT_LOCATION = 'פתח תקווה, ישראל'


class BackendSigner(signers.Signer):
    """A pyHanko signer whose private key lives behind a SigningBackend."""

    def __init__(self, backend: SigningBackend, certificate):
        self._backend = backend
        super().__init__(
            signing_cert=certificate,
            cert_registry=SimpleCertificateStore.from_certs([certificate]),
        )

    def _placeholder_size(self) -> int:
        # The dry run only sizes the hole the real signature goes into; a DER
        # ECDSA signature is at most 2·(n+1)+6 bytes, an RSA one exactly n.
        bits = self.signing_cert.public_key.bit_size
        if self.signing_cert.public_key.algorithm == 'ec':
            return 2 * (bits // 8 + 1) + 8
        return bits // 8

    async def async_sign_raw(self, data: bytes, digest_algorithm: str, dry_run: bool = False) -> bytes:
        if digest_algorithm != 'sha256':
            raise SigningError(f'Only SHA-256 is signed, not {digest_algorithm}')
        if dry_run:
            return bytes(self._placeholder_size())
        return self._backend.sign_digest(hashlib.sha256(data).digest(), 'sha256')


def validation_context(certificate) -> ValidationContext:
    """Trust exactly our own certificate, and fetch nothing from the network."""
    return ValidationContext(trust_roots=[certificate], allow_fetching=False)


def check_signed_pdf(pdf_bytes: bytes, certificate) -> None:
    """Raise SigningUnavailable unless the file carries one good signature over all of it."""
    try:
        reader = PdfFileReader(io.BytesIO(pdf_bytes))
        embedded = reader.embedded_signatures
        if len(embedded) != 1:
            raise SigningUnavailable(f'expected one signature, found {len(embedded)}')
        status = validate_pdf_signature(embedded[0], validation_context(certificate))
    except SigningUnavailable:
        raise
    except Exception as exc:
        raise SigningUnavailable(f'The signed file could not be validated: {type(exc).__name__}') from exc
    if not (status.intact and status.valid and status.trusted and status.bottom_line):
        raise SigningUnavailable(
            f'The signed file did not validate (intact={status.intact}, valid={status.valid}, '
            f'trusted={status.trusted})'
        )
    if status.coverage != SignatureCoverageLevel.ENTIRE_FILE:
        raise SigningUnavailable(f'The signature does not cover the whole file ({status.coverage})')


def sign_pdf(pdf_bytes: bytes, *, reason: str = DEFAULT_REASON, location: str = DEFAULT_LOCATION,
             backend: SigningBackend | None = None, certificate=None) -> bytes:
    """The PDF, signed (PAdES B-B, invisible field) and checked. SigningUnavailable on any failure."""
    backend = backend or get_backend()
    certificate = certificate if certificate is not None else backend.certificate()
    if certificate is None:
        raise SigningUnavailable('No signing certificate is configured')

    metadata = signers.PdfSignatureMetadata(
        field_name=FIELD_NAME,
        md_algorithm='sha256',
        subfilter=SigSeedSubFilter.PADES,
        reason=reason,
        location=location,
        name=ISSUER_NAME,
        contact_info=ISSUER_EMAIL,
    )
    try:
        writer = IncrementalPdfFileWriter(io.BytesIO(pdf_bytes))
        output = signers.PdfSigner(metadata, signer=BackendSigner(backend, certificate)).sign_pdf(writer)
        signed = output.getvalue()
    except SigningUnavailable:
        raise
    except Exception as exc:
        # pyHanko wraps a backend failure in its own errors; the cause is kept
        # for the log, the message stays free of anything but the type.
        cause = exc.__cause__ if isinstance(exc.__cause__, SigningUnavailable) else None
        if cause is not None:
            raise cause
        raise SigningUnavailable(f'Signing failed: {type(exc).__name__}') from exc

    check_signed_pdf(signed, certificate)
    return signed
