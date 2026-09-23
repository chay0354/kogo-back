"""
The signed original: drawn once, signed once, stored as signed, checked before it is used.

הוראה 1 (מסמך ממוחשב) and חוזר 24/2004 ("בצורתו המקורית, כולל החתימה").
LocalKeyBackend signs here; nothing reaches the network.
"""
import hashlib
import io
import os
from decimal import Decimal
from unittest.mock import patch

from asn1crypto import pem as asn1_pem, x509
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import validate_pdf_signature

from apps.documents import service
from apps.documents.document_pdf import build_document_layout, generate_document_pdf
from apps.documents.issuer import COPY_MARK, ORIGINAL_MARK, SIGNED_MARK
from apps.documents.models import FrozenSignedOriginalError, SignedOriginal
from apps.documents.signing import SigningUnavailable
from apps.documents.signing.backends import LocalKeyBackend, get_backend
from apps.documents.signing.certificate import fingerprint_sha256, load_certificate
from apps.documents.signing.service import (
    REASON_ARCHIVE, REASON_UNAVAILABLE, sign_original,
)
from apps.documents.signing.signer import check_signed_pdf, validation_context
from apps.documents.tests.signing_support import local_cert_pem, local_key_pem, pdf_text, signing_on
from apps.documents.tests.test_register import RegisterFixture


def notes_text(layout) -> str:
    return ' '.join(f'{note.lead} {note.text}' for note in layout.notes)


class CardReceiptMixin(RegisterFixture):
    def card_receipt(self, amount='100.00'):
        with self.captureOnCommitCallbacks(execute=True):
            return service.create_receipt({
                'client_type': 'existing',
                'child_id': str(self.kid.id),
                'document_date': '2026-09-18',
                'receipt_details': {
                    'payment_method': 'אשראי', 'card_amount': amount, 'card_last_four': '4242',
                },
            })


@signing_on()
class SignedOnceAtIssueTests(CardReceiptMixin, TestCase):
    def test_an_issued_document_is_signed_after_commit_and_its_bytes_stored_with_their_hash(self):
        doc = self.card_receipt()
        row = SignedOriginal.objects.get(number=doc.document_number)

        self.assertTrue(row.is_signed)
        self.assertEqual((row.kind, row.source_id), (SignedOriginal.KIND_FORMAL, str(doc.pk)))
        stored = bytes(row.pdf)
        self.assertEqual(row.sha256, hashlib.sha256(stored).hexdigest())
        self.assertEqual(row.size, len(stored))
        self.assertTrue(row.pdf_intact())
        self.assertTrue(row.key_id.startswith('local:'))
        self.assertEqual(row.cert_fingerprint, fingerprint_sha256(load_certificate()))
        # Validated by pyHanko against our own certificate: intact, valid, trusted, whole file.
        check_signed_pdf(stored, load_certificate())
        # A receipt kogo does not mail: its original goes into the archive.
        self.assertEqual((row.delivery, row.delivery_reason), (SignedOriginal.DELIVERY_NONE, REASON_ARCHIVE))
        self.assertEqual(row.document_type_label, 'קבלה')
        self.assertEqual(row.customer_name, self.kid.full_name)
        self.assertEqual(row.total, Decimal('100.00'))

    def test_the_original_says_original_and_signed_and_the_signature_is_invisible(self):
        doc = self.card_receipt()
        stored = bytes(SignedOriginal.objects.get(number=doc.document_number).pdf)
        text = pdf_text(stored)
        self.assertIn(ORIGINAL_MARK, text)
        self.assertIn('מסמך ממוחשב', text)
        self.assertIn(SIGNED_MARK, text)
        signature = PdfFileReader(io.BytesIO(stored)).embedded_signatures[0]
        self.assertEqual(signature.field_name, 'KogoSignature')
        rect = [float(v) for v in signature.sig_field.get('/Rect', [0, 0, 0, 0])]
        self.assertEqual(rect[2] - rect[0], 0)  # no box on the page
        self.assertEqual(signature.sig_object['/SubFilter'], '/ETSI.CAdES.detached')  # PAdES

    def test_signing_again_returns_the_same_original_and_never_signs_twice(self):
        doc = self.card_receipt()
        first = SignedOriginal.objects.get(number=doc.document_number)
        with patch.object(LocalKeyBackend, 'sign_digest') as sign:
            again = sign_original(SignedOriginal.KIND_FORMAL, doc)
            with self.captureOnCommitCallbacks(execute=True):
                service._sign_at_issue(doc)
        sign.assert_not_called()
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(again.sha256, first.sha256)
        self.assertEqual(SignedOriginal.objects.filter(number=doc.document_number).count(), 1)
        self.assertEqual(SignedOriginal.objects.get(pk=first.pk).sign_attempts, 1)

    def test_a_one_byte_change_fails_validation(self):
        doc = self.card_receipt()
        stored = bytearray(SignedOriginal.objects.get(number=doc.document_number).pdf)
        stored[200] = (stored[200] + 1) % 256
        with self.assertRaises(SigningUnavailable):
            check_signed_pdf(bytes(stored), load_certificate())
        status = validate_pdf_signature(
            PdfFileReader(io.BytesIO(bytes(stored))).embedded_signatures[0],
            validation_context(load_certificate()),
        )
        self.assertFalse(status.intact)

    def test_a_signed_original_is_never_rewritten_or_deleted(self):
        doc = self.card_receipt()
        row = SignedOriginal.objects.get(number=doc.document_number)
        row.pdf = b'%PDF-forged'
        with self.assertRaises(FrozenSignedOriginalError):
            row.save()
        with self.assertRaises(FrozenSignedOriginalError):
            SignedOriginal.objects.filter(pk=row.pk).update(pdf=b'%PDF-forged')
        with self.assertRaises(FrozenSignedOriginalError):
            SignedOriginal.objects.get(pk=row.pk).delete()
        with self.assertRaises(FrozenSignedOriginalError):
            SignedOriginal.objects.filter(pk=row.pk).delete()
        # Where it goes may still move.
        SignedOriginal.objects.filter(pk=row.pk).update(delivery_reason='x')

    def test_stored_bytes_that_no_longer_match_their_hash_are_caught(self):
        doc = self.card_receipt()
        row = SignedOriginal.objects.get(number=doc.document_number)
        with connection.cursor() as cursor:
            cursor.execute('UPDATE signed_originals SET pdf = %s WHERE id = %s', [b'%PDF-broken', row.pk])
        self.assertFalse(SignedOriginal.objects.get(pk=row.pk).pdf_intact())

    def test_a_draft_is_never_signed(self):
        with self.captureOnCommitCallbacks(execute=True):
            draft = service.create_draft({
                'client_type': 'existing', 'child_id': str(self.kid.id),
                'invoice_details': {'document_date': '2026-09-18', 'line_items': [{'description': 'x', 'quantity': 1, 'price': 10}]},
            })
        self.assertFalse(SignedOriginal.objects.filter(source_id=str(draft.pk)).exists())
        with self.captureOnCommitCallbacks(execute=True):
            final = service.finalize_draft(draft)
        self.assertTrue(SignedOriginal.objects.get(number=final.document_number).is_signed)


@signing_on()
class HeldWhenTheKeyIsOutOfReachTests(CardReceiptMixin, TestCase):
    def test_a_backend_failure_leaves_the_document_issued_and_its_original_held(self):
        with patch.object(LocalKeyBackend, 'sign_digest', side_effect=SigningUnavailable('KMS asymmetricSign refused (HTTP 503)')):
            doc = self.card_receipt()
        row = SignedOriginal.objects.get(number=doc.document_number)
        self.assertFalse(row.is_signed)
        self.assertIsNone(row.pdf)
        self.assertEqual((row.delivery, row.delivery_reason), (SignedOriginal.DELIVERY_HELD, REASON_UNAVAILABLE))
        self.assertIn('HTTP 503', row.last_error)
        self.assertEqual(row.sign_attempts, 1)
        # The document itself stands.
        doc.refresh_from_db()
        self.assertEqual(doc.total_amount, Decimal('100.00'))

    @signing_on(SIGNING_CERT_PEM='', SIGNING_LOCAL_KEY_PEM='')
    def test_no_key_or_certificate_holds_it_too(self):
        with patch('apps.documents.signing.certificate.CERT_PATH') as path:
            path.exists.return_value = False
            doc = self.card_receipt()
        row = SignedOriginal.objects.get(number=doc.document_number)
        self.assertEqual(row.delivery, SignedOriginal.DELIVERY_HELD)
        self.assertIn('No signing key', row.last_error)


class FlagOffTests(CardReceiptMixin, TestCase):
    def test_nothing_is_recorded_or_signed_while_signing_is_off(self):
        doc = self.card_receipt()
        self.assertFalse(SignedOriginal.objects.exists())
        self.assertIsNone(sign_original(SignedOriginal.KIND_FORMAL, doc))

    def test_the_documents_are_drawn_as_before(self):
        doc = self.card_receipt()
        layout = build_document_layout(doc)
        self.assertEqual(layout.copy_mark, ORIGINAL_MARK)
        self.assertNotIn(SIGNED_MARK, notes_text(layout))


class LayoutMarksTests(CardReceiptMixin, TestCase):
    def test_a_copy_says_copy_and_carries_no_signature_line(self):
        doc = self.card_receipt()
        copy = build_document_layout(doc, copy=True, signed=True)
        self.assertEqual(copy.copy_mark, COPY_MARK)
        self.assertNotIn(SIGNED_MARK, notes_text(copy))
        original = build_document_layout(doc, signed=True)
        self.assertEqual(original.copy_mark, ORIGINAL_MARK)
        self.assertIn(SIGNED_MARK, notes_text(original))
        self.assertIn(COPY_MARK, pdf_text(generate_document_pdf(doc, copy=True)))

    def test_a_draft_stays_a_draft_whatever_it_is_asked(self):
        draft = service.create_draft({
            'client_type': 'existing', 'child_id': str(self.kid.id),
            'invoice_details': {'document_date': '2026-09-18', 'line_items': [{'description': 'x', 'quantity': 1, 'price': 10}]},
        })
        self.assertEqual(build_document_layout(draft, copy=True).copy_mark, 'טיוטה — אינו מסמך מס')


class CertificateTests(TestCase):
    def backend(self):
        return LocalKeyBackend(local_key_pem())

    def test_make_signing_certificate_builds_a_certificate_the_backend_signs_under(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
        from cryptography.hazmat.primitives.serialization import load_der_public_key

        out = io.StringIO()
        with signing_on():
            call_command('make_signing_certificate', stdout=out, stderr=io.StringIO())
        text = out.getvalue()
        self.assertTrue(text.startswith('-----BEGIN CERTIFICATE-----'))
        _type, _headers, der = asn1_pem.unarmor(text.encode())
        certificate = x509.Certificate.load(der)

        subject = certificate.subject.native
        self.assertEqual(subject['organization_name'], 'קוגומלו גרופ בע"מ')
        self.assertEqual(subject['common_name'], 'קוגומלו גרופ בע"מ')
        self.assertEqual(subject['serial_number'], '516504412')
        self.assertEqual(subject['country_name'], 'IL')
        self.assertEqual(certificate.issuer.native, subject)  # self-issued
        self.assertEqual(certificate.key_usage_value.native, {'digital_signature', 'non_repudiation'})
        self.assertFalse(certificate.ca)
        validity = certificate['tbs_certificate']['validity']
        span = validity['not_after'].native - validity['not_before'].native
        self.assertEqual(span.days, 3650)

        # The certificate's key verifies what the backend signs.
        digest = hashlib.sha256(b'a document').digest()
        signature = self.backend().sign_digest(digest)
        public_key = load_der_public_key(certificate.public_key.dump())
        public_key.verify(signature, digest, ec.ECDSA(Prehashed(hashes.SHA256())))
        # And the certificate verifies its own signature (it is signed by that key).
        tbs_digest = hashlib.sha256(certificate['tbs_certificate'].dump()).digest()
        public_key.verify(certificate['signature_value'].native, tbs_digest, ec.ECDSA(Prehashed(hashes.SHA256())))

    def test_the_selftest_signs_and_validates_a_sample(self):
        out = io.StringIO()
        target = os.path.join(self._tmpdir(), 'selftest.pdf')
        with signing_on():
            call_command('signing_selftest', '--out', target, stdout=out)
        self.assertIn('Signing self-test passed', out.getvalue())
        with open(target, 'rb') as handle:
            check_signed_pdf(handle.read(), load_certificate_from_pem())

    def test_the_selftest_fails_loudly_without_a_certificate(self):
        from django.core.management.base import CommandError

        with signing_on(SIGNING_CERT_PEM=''), patch('apps.documents.signing.certificate.CERT_PATH') as path:
            path.exists.return_value = False
            with self.assertRaises(CommandError):
                call_command('signing_selftest', stdout=io.StringIO())

    def test_a_local_key_is_refused_on_vercel(self):
        with patch.dict(os.environ, {'VERCEL': '1'}):
            with self.assertRaises(SigningUnavailable):
                LocalKeyBackend(local_key_pem())
            with signing_on(), self.assertRaises(SigningUnavailable):
                get_backend()

    def test_a_pem_whose_newlines_arrived_escaped_still_loads(self):
        escaped = local_cert_pem().replace('\n', '\\n')
        with signing_on(SIGNING_CERT_PEM=escaped):
            self.assertIsNotNone(load_certificate())

    def _tmpdir(self):
        import tempfile

        path = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(path, ignore_errors=True))
        return path


def load_certificate_from_pem():
    _type, _headers, der = asn1_pem.unarmor(local_cert_pem().encode())
    return x509.Certificate.load(der)
