"""
Sign a sample document with the configured key and check it — before switching signing on.

    python manage.py signing_selftest [--out sample-signed.pdf]

Draws a sample through the same layout every document uses, signs it through
the configured backend (a real Cloud KMS call in production), validates the
result with pyHanko against our own certificate, and prints the key, the
certificate's fingerprint and the verdict. Writes nothing to the database and
mails nobody. Exits non-zero on any failure.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.documents.invoice_document import business_fields, computerized_note, footer_line, signature_note
from apps.documents.invoice_layout import Field, InvoiceLayout, LineItem, Note, render_invoice_pdf
from apps.documents.issuer import ISSUER_NAME, ORIGINAL_MARK
from apps.documents.signing import SigningUnavailable
from apps.documents.signing.backends import get_backend
from apps.documents.signing.certificate import certificate_matches_backend, fingerprint_sha256, subject_text
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


class Command(BaseCommand):
    help = 'Sign a sample PDF with the configured signing key and validate it (no database writes, no mail).'

    def add_arguments(self, parser):
        parser.add_argument('--out', help='Save the signed sample PDF here.')

    def handle(self, *args, **options):
        try:
            backend = get_backend()
            certificate = backend.certificate()
            if certificate is None:
                raise SigningUnavailable('No signing certificate is configured')
            if not certificate_matches_backend(certificate, backend):
                raise SigningUnavailable("The certificate is not the signing key's certificate")
            signed = sign_pdf(sample_pdf(), backend=backend, certificate=certificate)
            check_signed_pdf(signed, certificate)
        except SigningUnavailable as exc:
            raise CommandError(f'Signing self-test FAILED: {exc}') from exc

        if options.get('out'):
            with open(options['out'], 'wb') as handle:
                handle.write(signed)
            self.stdout.write(f'Signed sample: {options["out"]} ({len(signed)} bytes)')
        self.stdout.write(f'Backend: {backend.name}')
        self.stdout.write(f'Key: {backend.key_id}')
        self.stdout.write(f'Certificate: {subject_text(certificate)}')
        self.stdout.write(f'SHA-256 fingerprint: {fingerprint_sha256(certificate)}')
        self.stdout.write(self.style.SUCCESS('Signing self-test passed: intact, valid, trusted, whole file covered'))
