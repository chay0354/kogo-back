"""
Build the self-issued certificate of the signing key — once, when the key is created.

    python manage.py make_signing_certificate --out apps/documents/assets/signing_cert.pem

The certificate names the company (O and CN קוגומלו גרופ בע"מ, serialNumber
516504412, C=IL) and carries the key's public half; it is signed by the key
itself through the configured backend (Cloud KMS in production), so the private
key never leaves the HSM even for this. Only the public PEM is written. Commit
it, or put it in SIGNING_CERT_PEM, and publish its fingerprint.

Writes nothing to the database.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.documents.signing import SigningUnavailable
from apps.documents.signing.backends import get_backend
from apps.documents.signing.certificate import (
    VALIDITY_DAYS, build_self_issued_certificate, certificate_pem, fingerprint_sha256, subject_text,
)


class Command(BaseCommand):
    help = 'Build the self-issued X.509 certificate of the document-signing key (public PEM only).'

    def add_arguments(self, parser):
        parser.add_argument('--out', help='Write the PEM here instead of to stdout.')
        parser.add_argument('--days', type=int, default=VALIDITY_DAYS, help='Validity in days (default 3650).')
        parser.add_argument('--serial', type=int, default=None, help='Certificate serial number (default random).')

    def handle(self, *args, **options):
        try:
            backend = get_backend()
            certificate = build_self_issued_certificate(backend, days=options['days'], serial=options['serial'])
        except SigningUnavailable as exc:
            raise CommandError(f'The certificate could not be built: {exc}') from exc

        text = certificate_pem(certificate)
        if options.get('out'):
            with open(options['out'], 'w', encoding='ascii') as handle:
                handle.write(text)
            self.stderr.write(f'Wrote {options["out"]}')
        else:
            self.stdout.write(text, ending='')
        self.stderr.write(f'Key: {backend.key_id}')
        self.stderr.write(f'Subject: {subject_text(certificate)}')
        self.stderr.write(f'SHA-256 fingerprint: {fingerprint_sha256(certificate)}')
