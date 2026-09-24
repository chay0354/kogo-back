"""
The signing self-test: sign a sample through the configured key and check it.

Shared by `manage.py signing_selftest` and the setup endpoint, so the check a
developer runs locally is the one a deployment runs against Cloud KMS — which is
the only way to see the Vercel → Google handshake work before real documents
depend on it. Writes nothing to the database and mails nobody.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone

from apps.documents.invoice_document import business_fields, computerized_note, footer_line, signature_note
from apps.documents.invoice_layout import Field, InvoiceLayout, LineItem, Note, render_invoice_pdf
from apps.documents.issuer import ISSUER_NAME, ORIGINAL_MARK
from apps.documents.signing import SigningUnavailable
from apps.documents.signing.backends import get_backend
from apps.documents.signing.certificate import certificate_matches_backend
from apps.documents.signing.signer import check_signed_pdf, sign_pdf


def sample_pdf() -> bytes:
    stamp = timezone.localtime().strftime('%d/%m/%Y %H:%M')
    return render_invoice_pdf(InvoiceLayout(
        title='בדיקת חתימה - אינו מסמך מס',
        copy_mark=ORIGINAL_MARK,
        document_fields=[Field('מספר מסמך', 'TEST-SIGNATURE'), Field('תאריך ושעה', stamp)],
        business_fields=business_fields(),
        items=[LineItem(description='בדיקת חתימה אלקטרונית', quantity='1', unit_price='0.00',
                        line_net='0.00', vat_rate='0%', line_gross='0.00')],
        grand_value='0.00',
        notes=[Note('בדיקה:', 'קובץ לבדיקת החתימה בלבד. אינו מסמך מס.'), computerized_note(), signature_note()],
        footer=footer_line(),
        pdf_title='TEST-SIGNATURE',
        pdf_author=ISSUER_NAME,
    ))


@dataclass
class SelftestResult:
    pdf: bytes
    backend: object
    certificate: object


def run_selftest() -> SelftestResult:
    """Sign the sample and validate it; SigningUnavailable says what is missing or wrong."""
    backend = get_backend()
    certificate = backend.certificate()
    if certificate is None:
        raise SigningUnavailable('No signing certificate is configured')
    if not certificate_matches_backend(certificate, backend):
        raise SigningUnavailable("The certificate is not the signing key's certificate")
    signed = sign_pdf(sample_pdf(), backend=backend, certificate=certificate)
    check_signed_pdf(signed, certificate)
    return SelftestResult(pdf=signed, backend=backend, certificate=certificate)
