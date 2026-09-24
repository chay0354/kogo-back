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

from apps.documents.signing import SigningUnavailable
from apps.documents.signing.certificate import fingerprint_sha256, subject_text
from apps.documents.signing.selftest import run_selftest


class Command(BaseCommand):
    help = 'Sign a sample PDF with the configured signing key and validate it (no database writes, no mail).'

    def add_arguments(self, parser):
        parser.add_argument('--out', help='Save the signed sample PDF here.')

    def handle(self, *args, **options):
        try:
            result = run_selftest()
        except SigningUnavailable as exc:
            raise CommandError(f'Signing self-test FAILED: {exc}') from exc
        signed, backend, certificate = result.pdf, result.backend, result.certificate

        if options.get('out'):
            with open(options['out'], 'wb') as handle:
                handle.write(signed)
            self.stdout.write(f'Signed sample: {options["out"]} ({len(signed)} bytes)')
        self.stdout.write(f'Backend: {backend.name}')
        self.stdout.write(f'Key: {backend.key_id}')
        self.stdout.write(f'Certificate: {subject_text(certificate)}')
        self.stdout.write(f'SHA-256 fingerprint: {fingerprint_sha256(certificate)}')
        self.stdout.write(self.style.SUCCESS('Signing self-test passed: intact, valid, trusted, whole file covered'))
