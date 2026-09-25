"""
The signed archive: a copy, signed and stored, of every document issued before signing existed.

The customer's "מקור" left when the document was issued, and the software must
never produce "מקור" twice (תקנה 9א(א)(2), הוראה 18(ב)(2)) — so the copy says
"העתק לארכיון", is never mailed, never on the hand-delivery list, and never
printed as an original. It has a switch of its own, SIGNING_ARCHIVE_ENABLED,
that works while DOCUMENT_SIGNING_ENABLED is still off. LocalKeyBackend signs;
nothing reaches the network.
"""
import csv
import hashlib
import io
import uuid
import zipfile
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase, override_settings
from django.utils import timezone
from pyhanko.pdf_utils.reader import PdfFileReader
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.customers.financial_models import Invoice
from apps.customers.subscription_invoice_pdf import build_subscription_invoice_layout
from apps.documents import service
from apps.documents.document_pdf import build_document_layout
from apps.documents.invoice_document import edition
from apps.documents.invoice_layout import _signature_seal, ensure_fonts_registered, FONT_BOLD
from apps.documents.issuer import ARCHIVE_MARK, COPY_MARK, ORIGINAL_MARK, SIGNED_MARK
from apps.documents.models import FormalDocument, FrozenSignedOriginalError, SignedFileAccess, SignedOriginal
from apps.documents.signature_seal import CENTRE_MAX_WIDTH, CENTRE_SIZE, CENTRE_TEXT, CENTRE_TRACKING, _outlines
from apps.documents.signing import SigningUnavailable
from apps.documents.signing.archive import (
    FAILED_CLASH, REASON_ARCHIVE_COPY, SIGNATURE_REASON, ArchiveDisabled, archive_candidates, archive_status,
    eligible, run_archive_batch, sign_archive,
)
from apps.documents.signing.backends import LocalKeyBackend
from apps.documents.signing.certificate import fingerprint_sha256, load_certificate
from apps.documents.signing.service import ARCHIVE_NOT_ORIGINAL, claim_email, sign_original, sign_pending
from apps.documents.signing.signer import check_signed_pdf
from apps.documents.signing.views import ARCHIVE_DISABLED, ARCHIVE_UNAVAILABLE, FILE_NOT_FOUND
from apps.documents.tests.signing_support import archive_on, pdf_text, signing_on
from apps.documents.tests.test_register import RegisterFixture, make_user
from apps.store.invoice_pdf import build_store_invoice_layout
from apps.store.models import StoreInvoice

IR = SignedOriginal.KIND_IR
STORE = SignedOriginal.KIND_STORE
FORMAL = SignedOriginal.KIND_FORMAL
ARCHIVE = SignedOriginal.PURPOSE_ARCHIVE

ORIGINALS = '/api/v1/documents/signing/originals/'
EXPORT = '/api/v1/documents/signing/originals/export/'
STATUS = '/api/v1/documents/signing/status/'
ARCHIVE_STATUS = '/api/v1/documents/signing/archive/status/'
ARCHIVE_RUN = '/api/v1/documents/signing/archive/run/'

EMAIL_EXITS = (
    'apps.customers.subscription_invoice_email.send_resend_email',
    'apps.store.invoice_email.send_resend_email',
    'apps.core.credit_note_email.send_resend_email',
    'apps.rental_billing.receipt_email.send_resend_email',
)


def file_url(row) -> str:
    return f'{ORIGINALS}{row.pk}/file/'


def print_url(row) -> str:
    return f'{ORIGINALS}{row.pk}/print-original/'


def notes_text(layout) -> str:
    return ' '.join(f'{note.lead} {note.text}' for note in layout.notes)


def text_lines(pdf_bytes: bytes) -> list[str]:
    return [line.strip() for line in pdf_text(pdf_bytes).splitlines()]


def signature_reason(pdf_bytes: bytes) -> str:
    return str(PdfFileReader(io.BytesIO(pdf_bytes)).embedded_signatures[0].sig_object.get('/Reason', ''))


class ArchiveFixture(RegisterFixture):
    """Documents as they stand today: issued while signing was off, so none has a row."""

    def receipt(self, method='אשראי', amount='100.00', day='2026-08-18'):
        amounts = {'מזומן': 'cash_amount', 'אשראי': 'card_amount'}
        with self.captureOnCommitCallbacks(execute=True):
            return service.create_receipt({
                'client_type': 'existing', 'child_id': str(self.kid.id), 'document_date': day,
                'receipt_details': {'payment_method': method, amounts[method]: amount, 'card_last_four': '4242'},
            })

    def three_documents(self):
        """One of each kind: a lesson receipt, a store sale and a hand-issued receipt."""
        lesson = self.lesson_receipt('IR-2026-000001', day=9)
        sale = self.store_sale(day=10)
        doc = self.receipt(day='2026-08-11')
        return lesson, sale, doc


# ── what an archive copy looks like ─────────────────────────────────────────

class EditionTests(TestCase):
    def test_archive_wins_over_copy_and_signed_and_is_never_the_original(self):
        for copy in (False, True):
            for signed in (False, True):
                print_as = edition(copy=copy, signed=signed, archive=True)
                self.assertEqual(print_as.copy_mark, ARCHIVE_MARK)
                self.assertTrue(print_as.seal)
                self.assertEqual(print_as.seal_centre_text, ARCHIVE_MARK)
                leads = [note.lead for note in print_as.closing_notes]
                self.assertEqual(leads, ['חתימה אלקטרונית:', 'העתק לארכיון:'])

    def test_the_other_prints_are_unchanged(self):
        self.assertEqual(edition(), edition(copy=False, signed=False, archive=False))
        self.assertEqual((edition().copy_mark, edition().seal), (ORIGINAL_MARK, False))
        self.assertEqual((edition(copy=True, signed=True).copy_mark, edition(copy=True, signed=True).seal),
                         (COPY_MARK, False))
        signed = edition(signed=True)
        self.assertEqual((signed.copy_mark, signed.seal, signed.seal_centre_text), (ORIGINAL_MARK, True, ''))
        self.assertEqual([note.text for note in signed.closing_notes], [f'{SIGNED_MARK}.'])


class ArchiveRenderTests(ArchiveFixture, TestCase):
    def assert_archive_layout(self, layout):
        self.assertEqual(layout.copy_mark, ARCHIVE_MARK)
        self.assertNotEqual(layout.copy_mark, ORIGINAL_MARK)
        text = notes_text(layout)
        self.assertIn(SIGNED_MARK, text)
        today = timezone.localdate().strftime('%d/%m/%Y')
        self.assertIn(
            f'הופק מחדש מנתוני המסמך ביום {today} ונחתם לשמירה בארכיון. אינו המקור שנמסר ללקוח.', text,
        )
        self.assertEqual(layout.notes[-1].lead, 'העתק לארכיון:')
        self.assertTrue(layout.signed_seal)
        self.assertEqual(layout.seal_centre_text, ARCHIVE_MARK)
        # What the document says stays: the computerized-document line is still there.
        self.assertIn('מסמך ממוחשב:', [note.lead for note in layout.notes])

    def test_each_generator_draws_the_archive_copy(self):
        lesson, sale, doc = self.three_documents()
        lesson = Invoice.objects.select_related('family', 'branch', 'payment').get(pk=lesson.pk)
        self.assert_archive_layout(build_subscription_invoice_layout(lesson, archive=True))
        self.assert_archive_layout(build_store_invoice_layout(sale, archive=True))
        self.assert_archive_layout(build_document_layout(doc, archive=True))
        # Even when asked for a copy or the signed original too.
        self.assert_archive_layout(build_document_layout(doc, copy=True, signed=True, archive=True))

    def test_a_draft_is_never_an_archive_copy(self):
        draft = service.create_draft({
            'client_type': 'existing', 'child_id': str(self.kid.id),
            'invoice_details': {'document_date': '2026-08-18',
                                'line_items': [{'description': 'x', 'quantity': 1, 'price': 10}]},
        })
        layout = build_document_layout(draft, archive=True)
        self.assertEqual(layout.copy_mark, 'טיוטה — אינו מסמך מס')
        self.assertFalse(layout.signed_seal)
        self.assertEqual(layout.seal_centre_text, '')

    def test_the_rendered_archive_copy_never_says_original_as_its_mark(self):
        from apps.documents.signing.sources import load_source

        for kind, obj in zip((IR, STORE, FORMAL), self.three_documents()):
            pdf = load_source(kind, obj.pk).render_archive()
            lines = text_lines(pdf)
            self.assertIn(ARCHIVE_MARK, lines, kind)
            self.assertNotIn(ORIGINAL_MARK, lines, kind)
            self.assertNotIn(COPY_MARK, lines, kind)
            self.assertIn(SIGNED_MARK, pdf_text(pdf), kind)
            # The check means something: the same document's original does carry the line.
            self.assertIn(ORIGINAL_MARK, text_lines(load_source(kind, obj.pk).render_original()), kind)

    def test_the_seal_says_archive_copy_in_its_centre_and_it_fits(self):
        ensure_fonts_registered()
        outlines = _outlines(FONT_BOLD)

        def width(text, size):
            return sum(outlines.advance(char, size) for char in text) + CENTRE_TRACKING * (len(text) - 1)

        archive_seal = _signature_seal(ARCHIVE_MARK)
        self.assertEqual(archive_seal.centre_text, ARCHIVE_MARK)
        self.assertLessEqual(width(ARCHIVE_MARK, archive_seal._centre_size(outlines)), CENTRE_MAX_WIDTH + 1e-6)
        default = _signature_seal()
        self.assertEqual(default.centre_text, CENTRE_TEXT)
        # The original's seal is drawn exactly as before.
        self.assertEqual(default._centre_size(outlines), CENTRE_SIZE)


# ── signing into the archive ────────────────────────────────────────────────

@archive_on()
class SignArchiveTests(ArchiveFixture, TestCase):
    def test_the_customer_facing_switch_is_off_and_the_archive_still_signs(self):
        from django.conf import settings

        self.assertFalse(settings.DOCUMENT_SIGNING_ENABLED)
        lesson = self.lesson_receipt('IR-2026-000001')
        self.assertIsNotNone(sign_archive(IR, lesson))

    def test_an_archive_copy_is_stored_signed_and_verifiable(self):
        lesson, sale, doc = self.three_documents()
        for kind, obj in ((IR, lesson), (STORE, sale), (FORMAL, doc)):
            row = sign_archive(kind, obj)
            self.assertEqual((row.kind, row.source_id, row.purpose), (kind, str(obj.pk), ARCHIVE))
            self.assertEqual((row.channel, row.delivery, row.delivery_reason),
                             ('', SignedOriginal.DELIVERY_NONE, REASON_ARCHIVE_COPY))
            stored = bytes(row.pdf)
            self.assertEqual(row.sha256, hashlib.sha256(stored).hexdigest())
            self.assertEqual(row.size, len(stored))
            self.assertTrue(row.pdf_intact())
            self.assertTrue(row.key_id.startswith('local:'))
            self.assertEqual(row.cert_fingerprint, fingerprint_sha256(load_certificate()))
            self.assertIsNotNone(row.signed_at)
            check_signed_pdf(stored, load_certificate())
            # The signature itself does not say "מקור" either.
            self.assertEqual(signature_reason(stored), SIGNATURE_REASON)
            self.assertNotIn(ORIGINAL_MARK, signature_reason(stored))
            lines = text_lines(stored)
            self.assertIn(ARCHIVE_MARK, lines)
            self.assertNotIn(ORIGINAL_MARK, lines)
        self.assertEqual(
            SignedOriginal.objects.get(number=doc.document_number).customer_name, self.kid.full_name,
        )

    def test_signing_again_skips_and_never_signs_twice(self):
        lesson = self.lesson_receipt('IR-2026-000001')
        first = sign_archive(IR, lesson)
        with patch.object(LocalKeyBackend, 'sign_digest') as sign:
            self.assertIsNone(sign_archive(IR, lesson))
        sign.assert_not_called()
        self.assertEqual(SignedOriginal.objects.filter(number='IR-2026-000001').count(), 1)
        self.assertEqual(SignedOriginal.objects.get(pk=first.pk).sha256, first.sha256)

    def test_a_document_with_an_original_is_never_archived(self):
        with signing_on():
            doc = self.receipt()
        original = SignedOriginal.objects.get(number=doc.document_number)
        self.assertTrue(original.is_signed)
        self.assertNotEqual(original.purpose, ARCHIVE)
        with patch.object(LocalKeyBackend, 'sign_digest') as sign:
            self.assertIsNone(sign_archive(FORMAL, doc))
        sign.assert_not_called()
        self.assertFalse(archive_candidates(FORMAL).filter(pk=doc.pk).exists())
        self.assertEqual(SignedOriginal.objects.get(pk=original.pk).purpose, original.purpose)

    def test_nothing_is_mailed_or_marked_sent(self):
        lesson, sale, doc = self.three_documents()
        sale.customer_email = 'buyer@example.com'
        sale.save(update_fields=['customer_email'])
        patches = [patch(target) for target in EMAIL_EXITS]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        result = run_archive_batch(limit=10)
        self.assertEqual(result['signed'], 3)
        for mock in mocks:
            mock.assert_not_called()
        self.assertEqual(mail.outbox, [])
        self.assertFalse(SignedOriginal.objects.filter(sent_at__isnull=False).exists())
        self.assertFalse(SignedOriginal.objects.exclude(channel='').exists())
        self.assertIsNone(Invoice.objects.get(pk=lesson.pk).email_sent_at)
        self.assertIsNone(StoreInvoice.objects.get(pk=sale.pk).invoice_email_sent_at)

    def test_a_cash_receipt_is_never_on_the_paper_list(self):
        doc = self.receipt('מזומן')
        row = sign_archive(FORMAL, doc)
        self.assertEqual(row.delivery, SignedOriginal.DELIVERY_NONE)
        self.assertIsNone(row.paper_original_printed_at)

    def test_print_original_refuses_an_archive_copy(self):
        from apps.documents.signing.service import PrintRefused, print_original

        row = sign_archive(FORMAL, self.receipt('מזומן'))
        with self.assertRaises(PrintRefused) as refused:
            print_original(row.pk, self.manager)
        self.assertEqual((str(refused.exception), refused.exception.status), (ARCHIVE_NOT_ORIGINAL, 409))
        row.refresh_from_db()
        self.assertIsNone(row.paper_original_printed_at)
        self.assertEqual(row.delivery, SignedOriginal.DELIVERY_NONE)

    def test_once_signing_is_on_an_archived_document_is_never_given_an_original(self):
        lesson = self.lesson_receipt('IR-2026-000001')
        row = sign_archive(IR, lesson)
        with signing_on(), patch('apps.customers.subscription_invoice_email.send_resend_email') as resend:
            self.assertIsNone(sign_original(IR, lesson, channel=SignedOriginal.CHANNEL_IR, email_to='p@example.com'))
            self.assertIsNone(claim_email(IR, lesson, channel=SignedOriginal.CHANNEL_IR, email_to='p@example.com'))
            from apps.customers.subscription_invoice_email import send_subscription_invoice_email

            self.assertFalse(send_subscription_invoice_email(Invoice.objects.get(pk=lesson.pk)))
            summary = sign_pending()
        resend.assert_not_called()
        self.assertEqual((summary['signed'], summary['sent']), (0, 0))
        after = SignedOriginal.objects.get(pk=row.pk)
        self.assertEqual((after.purpose, after.channel, after.email_to, after.sent_at, after.sha256),
                         (ARCHIVE, '', '', None, row.sha256))

    def test_a_failed_signature_leaves_no_row(self):
        lesson = self.lesson_receipt('IR-2026-000001')
        with patch.object(LocalKeyBackend, 'sign_digest', side_effect=SigningUnavailable('KMS down (HTTP 503)')):
            with self.assertRaises(SigningUnavailable):
                sign_archive(IR, lesson)
        with patch('apps.customers.subscription_invoice_pdf.render_invoice_pdf', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                sign_archive(IR, lesson)
        self.assertFalse(SignedOriginal.objects.exists())

    @archive_on(SIGNING_CERT_PEM='', SIGNING_LOCAL_KEY_PEM='')
    def test_no_key_or_certificate_signs_nothing(self):
        lesson = self.lesson_receipt('IR-2026-000001')
        with patch('apps.documents.signing.certificate.CERT_PATH') as path:
            path.exists.return_value = False
            with self.assertRaises(SigningUnavailable):
                sign_archive(IR, lesson)
        self.assertFalse(SignedOriginal.objects.exists())

    @override_settings(SIGNING_ARCHIVE_ENABLED=False)
    def test_the_switch_off_refuses(self):
        lesson = self.lesson_receipt('IR-2026-000001')
        with self.assertRaises(ArchiveDisabled):
            sign_archive(IR, lesson)
        with self.assertRaises(ArchiveDisabled):
            run_archive_batch()
        self.assertFalse(SignedOriginal.objects.exists())

    def test_an_archive_copy_never_becomes_an_original(self):
        row = sign_archive(IR, self.lesson_receipt('IR-2026-000001'))
        row.purpose = SignedOriginal.PURPOSE_ORIGINAL
        with self.assertRaises(FrozenSignedOriginalError):
            row.save()
        with self.assertRaises(FrozenSignedOriginalError):
            SignedOriginal.objects.filter(pk=row.pk).update(purpose=SignedOriginal.PURPOSE_ORIGINAL)
        with self.assertRaises(FrozenSignedOriginalError):
            SignedOriginal.objects.get(pk=row.pk).delete()


# ── which documents: the register's rules ───────────────────────────────────

@archive_on()
class EligibilityTests(ArchiveFixture, TestCase):
    def numbers(self, kind, **kwargs):
        return [getattr(obj, 'invoice_number', None) or obj.document_number
                for obj in archive_candidates(kind, **kwargs)]

    def test_lesson_receipts_in_the_ir_run_whose_charge_went_through(self):
        self.lesson_receipt('IR-2026-000001')
        void = self.lesson_receipt('IR-2026-000002')
        Invoice.objects.filter(pk=void.pk).update(status='pending')
        failed = self.lesson_receipt('IR-2026-000003')
        Invoice.objects.filter(pk=failed.pk).update(status='failed')
        old = self.lesson_receipt('INV-20260809-A1B2C3D4')
        self.assertEqual(self.numbers(IR), ['IR-2026-000001'])
        self.assertIsNone(sign_archive(IR, old))
        self.assertIsNone(sign_archive(IR, void))
        self.assertFalse(SignedOriginal.objects.exists())

    def test_store_sales_as_the_signing_service_reads_them(self):
        paid = self.store_sale(day=10)
        refunded = self.store_sale(day=11, status='refunded')
        monthly = self.store_sale(day=12, method='monthly_billing', status='pending')
        pending = self.store_sale(day=13, status='pending', branch=None, website_order_number='CG-260813-TEST')
        failed = self.store_sale(day=14, status='failed')
        self.assertEqual(
            self.numbers(STORE), [monthly.invoice_number, refunded.invoice_number, paid.invoice_number],
        )
        self.assertIsNone(sign_archive(STORE, pending))
        self.assertIsNone(sign_archive(STORE, failed))

    def test_a_draft_is_never_archived(self):
        draft = service.create_draft({
            'client_type': 'existing', 'child_id': str(self.kid.id),
            'invoice_details': {'document_date': '2026-08-18',
                                'line_items': [{'description': 'x', 'quantity': 1, 'price': 10}]},
        })
        credit = self.credit_note()
        receipt = self.receipt()
        self.assertEqual(set(self.numbers(FORMAL)), {credit.document_number, receipt.document_number})
        self.assertIsNone(sign_archive(FORMAL, draft))

    def test_a_sale_with_a_tranzila_copy_is_archived_once_as_the_sale(self):
        sale = self.store_sale()
        copy = FormalDocument.objects.create(
            document_number='1001', document_type='combined', client_type='existing', branch=self.north,
            document_date=date(2026, 8, 10), subtotal=Decimal('49.00'), total_amount=Decimal('49.00'),
        )
        sale.formal_document = copy
        sale.save(update_fields=['formal_document'])
        self.assertEqual(self.numbers(FORMAL), [])
        self.assertFalse(eligible(FORMAL).filter(pk=copy.pk).exists())
        result = run_archive_batch(limit=10)
        self.assertEqual((result['signed'], result['remaining'], result['done']), (1, 0, True))
        self.assertEqual(list(SignedOriginal.objects.values_list('number', 'kind')), [(sale.invoice_number, STORE)])

    def test_newest_first_by_document_date_and_since(self):
        self.lesson_receipt('IR-2026-000001', day=3)
        self.lesson_receipt('IR-2026-000002', day=20)
        self.lesson_receipt('IR-2026-000003', day=11)
        self.assertEqual(self.numbers(IR), ['IR-2026-000002', 'IR-2026-000003', 'IR-2026-000001'])
        self.assertEqual(self.numbers(IR, since=date(2026, 8, 11)), ['IR-2026-000002', 'IR-2026-000003'])


# ── a batch ─────────────────────────────────────────────────────────────────

@archive_on()
class BatchTests(ArchiveFixture, TestCase):
    def test_the_limit_is_kept_and_the_next_run_finishes(self):
        for n in range(1, 4):
            self.lesson_receipt(f'IR-2026-00000{n}', day=n)
        first = run_archive_batch(limit=2)
        self.assertEqual(
            {k: first[k] for k in ('signed', 'skipped', 'failed', 'remaining', 'done', 'unavailable')},
            {'signed': 2, 'skipped': 0, 'failed': [], 'remaining': 1, 'done': False, 'unavailable': ''},
        )
        # Newest first.
        self.assertEqual(set(SignedOriginal.objects.values_list('number', flat=True)),
                         {'IR-2026-000003', 'IR-2026-000002'})
        second = run_archive_batch(limit=2)
        self.assertEqual((second['signed'], second['remaining'], second['done']), (1, 0, True))
        third = run_archive_batch(limit=2)
        self.assertEqual((third['signed'], third['remaining'], third['done']), (0, 0, True))

    def test_a_number_clash_is_a_failure_and_the_batch_goes_on(self):
        clashing = self.lesson_receipt('IR-2026-000002', day=20)
        self.lesson_receipt('IR-2026-000001', day=5)
        SignedOriginal.objects.create(number='IR-2026-000002', kind=FORMAL, source_id=str(uuid.uuid4()))
        result = run_archive_batch(limit=10)
        self.assertEqual(result['signed'], 1)
        self.assertEqual(result['failed'], [{'number': clashing.invoice_number, 'kind': IR, 'error': FAILED_CLASH}])
        self.assertEqual((result['remaining'], result['done']), (1, True))
        self.assertTrue(SignedOriginal.objects.get(number='IR-2026-000001').is_archive_copy)
        self.assertFalse(SignedOriginal.objects.get(number='IR-2026-000002').is_archive_copy)

    def test_one_document_that_cannot_be_drawn_does_not_stop_the_others(self):
        self.lesson_receipt('IR-2026-000001')
        doc = self.receipt()
        with patch('apps.customers.subscription_invoice_pdf.render_invoice_pdf', side_effect=RuntimeError('boom')):
            result = run_archive_batch(limit=10)
        self.assertEqual(result['signed'], 1)
        self.assertEqual([(f['number'], f['kind']) for f in result['failed']], [('IR-2026-000001', IR)])
        self.assertIn('RuntimeError', result['failed'][0]['error'])
        self.assertTrue(SignedOriginal.objects.get(number=doc.document_number).is_archive_copy)
        self.assertFalse(SignedOriginal.objects.filter(number='IR-2026-000001').exists())

    def test_the_key_out_of_reach_stops_the_batch(self):
        self.three_documents()
        with patch.object(LocalKeyBackend, 'sign_digest', side_effect=SigningUnavailable('KMS down')) as sign:
            result = run_archive_batch(limit=10)
        self.assertEqual(sign.call_count, 1)
        self.assertEqual((result['signed'], result['failed'], result['done']), (0, [], False))
        self.assertEqual(result['unavailable'], 'KMS down')
        self.assertEqual(result['remaining'], 3)
        self.assertFalse(SignedOriginal.objects.exists())

    @archive_on(SIGNING_CERT_PEM='', SIGNING_LOCAL_KEY_PEM='')
    def test_no_key_at_all_touches_no_document(self):
        self.three_documents()
        with patch('apps.documents.signing.certificate.CERT_PATH') as path, \
                patch('apps.documents.signing.archive.sign_archive') as sign:
            path.exists.return_value = False
            result = run_archive_batch()
        sign.assert_not_called()
        self.assertIn('No signing key', result['unavailable'])
        self.assertEqual((result['signed'], result['remaining']), (0, 3))

    def test_the_time_budget_stops_after_at_least_one(self):
        self.three_documents()
        result = run_archive_batch(limit=10, time_budget_seconds=0)
        self.assertEqual((result['signed'], result['remaining'], result['done']), (1, 2, False))

    def test_since(self):
        self.lesson_receipt('IR-2026-000001', day=3)
        self.lesson_receipt('IR-2026-000002', day=20)
        result = run_archive_batch(limit=10, since=date(2026, 8, 10))
        self.assertEqual((result['signed'], result['remaining'], result['done']), (1, 0, True))
        self.assertEqual(list(SignedOriginal.objects.values_list('number', flat=True)), ['IR-2026-000002'])


@archive_on()
class ArchiveStatusTests(ArchiveFixture, TestCase):
    def test_the_counts_per_kind(self):
        lesson, sale, doc = self.three_documents()
        self.lesson_receipt('IR-2026-000002', day=12)
        with signing_on():
            original = self.receipt()
        sign_archive(IR, lesson)
        status = archive_status()
        by_kind = {row['kind']: row for row in status['kinds']}
        self.assertEqual([row['kind'] for row in status['kinds']], [IR, STORE, FORMAL])
        self.assertEqual(by_kind[IR], {'kind': IR, 'label': 'קבלת חוג', 'eligible': 2, 'archived': 1,
                                       'originals': 0, 'remaining': 1})
        self.assertEqual(by_kind[STORE], {'kind': STORE, 'label': 'מכירת חנות', 'eligible': 1, 'archived': 0,
                                          'originals': 0, 'remaining': 1})
        self.assertEqual(by_kind[FORMAL], {'kind': FORMAL, 'label': 'מסמך', 'eligible': 2, 'archived': 0,
                                           'originals': 1, 'remaining': 1})
        self.assertEqual(status['last_signed_at'], SignedOriginal.objects.get(number=lesson.invoice_number).signed_at)
        self.assertTrue(SignedOriginal.objects.get(number=original.document_number).is_signed)


# ── the API ─────────────────────────────────────────────────────────────────

class ArchiveApiMixin(ArchiveFixture):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.manager)


@archive_on()
class ArchiveEndpointsTests(ArchiveApiMixin, APITestCase):
    def test_status(self):
        lesson, _sale, _doc = self.three_documents()
        sign_archive(IR, lesson)
        body = self.client.get(ARCHIVE_STATUS).json()
        self.assertTrue(body['enabled'])
        self.assertEqual([row['kind'] for row in body['kinds']], [IR, STORE, FORMAL])
        self.assertEqual(body['kinds'][0]['archived'], 1)
        self.assertIsNotNone(body['last_signed_at'])

    def test_run(self):
        self.three_documents()
        response = self.client.post(ARCHIVE_RUN, {'limit': 2}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {'signed': 2, 'skipped': 0, 'failed': [], 'remaining': 1,
                                           'done': False, 'unavailable': ''})
        response = self.client.post(ARCHIVE_RUN, {}, format='json')
        self.assertEqual((response.json()['signed'], response.json()['done']), (1, True))

    def test_the_limit_is_capped_and_since_is_checked(self):
        with patch('apps.documents.signing.archive.run_archive_batch', return_value={
            'signed': 0, 'skipped': 0, 'failed': [], 'remaining': 0, 'done': True, 'unavailable': '',
        }) as run:
            self.client.post(ARCHIVE_RUN, {'limit': 500, 'since': '2026-08-10'}, format='json')
        run.assert_called_once_with(limit=50, since=date(2026, 8, 10))
        bad = self.client.post(ARCHIVE_RUN, {'since': '10/08/2026'}, format='json')
        self.assertEqual(bad.status_code, 400)

    @override_settings(SIGNING_ARCHIVE_ENABLED=False)
    def test_the_switch_off_is_409(self):
        self.three_documents()
        response = self.client.post(ARCHIVE_RUN, {}, format='json')
        self.assertEqual((response.status_code, response.json()), (409, {'error': ARCHIVE_DISABLED}))
        self.assertFalse(self.client.get(ARCHIVE_STATUS).json()['enabled'])
        self.assertFalse(SignedOriginal.objects.exists())

    def test_the_key_out_of_reach_is_503(self):
        self.three_documents()
        with patch.object(LocalKeyBackend, 'sign_digest', side_effect=SigningUnavailable('KMS down')):
            response = self.client.post(ARCHIVE_RUN, {}, format='json')
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual((body['error'], body['unavailable'], body['signed']), (ARCHIVE_UNAVAILABLE, 'KMS down', 0))

    def test_the_status_screen_counts_originals_only(self):
        sign_archive(IR, self.lesson_receipt('IR-2026-000001'))
        counts = self.client.get(STATUS).json()
        self.assertEqual(counts['counts']['signed_today'], 0)
        self.assertIsNone(counts['last_signed_at'])


@archive_on()
class OriginalsListTests(ArchiveApiMixin, APITestCase):
    def setUp(self):
        super().setUp()
        lesson, sale, self.doc = self.three_documents()
        self.lesson_row = sign_archive(IR, lesson)
        self.sale_row = sign_archive(STORE, sale)
        with signing_on():
            self.original_doc = self.receipt('מזומן', day='2026-08-25')
        self.original_row = SignedOriginal.objects.get(number=self.original_doc.document_number)

    def numbers(self, **params):
        response = self.client.get(ORIGINALS, params)
        self.assertEqual(response.status_code, 200, response.content)
        return {row['number'] for row in response.json()['results']}

    def test_every_row_carries_what_it_is(self):
        results = {row['number']: row for row in self.client.get(ORIGINALS).json()['results']}
        row = results[self.lesson_row.number]
        self.assertEqual(set(row), {
            'id', 'number', 'kind', 'purpose', 'document_type_label', 'customer_name', 'document_date', 'total',
            'delivery', 'delivery_reason', 'signed_at', 'sent_at', 'paper_original_printed_at', 'sha256', 'size',
        })
        self.assertEqual((row['purpose'], row['kind'], row['sha256'], row['size'], row['delivery']),
                         (ARCHIVE, IR, self.lesson_row.sha256, self.lesson_row.size, 'none'))
        self.assertEqual(row['document_date'], '2026-08-09')
        self.assertEqual(results[self.original_row.number]['purpose'], 'original')

    def test_filters(self):
        archived = {self.lesson_row.number, self.sale_row.number}
        self.assertEqual(self.numbers(purpose='archive'), archived)
        self.assertEqual(self.numbers(purpose='original'), {self.original_row.number})
        self.assertEqual(self.numbers(kind='store'), {self.sale_row.number})
        self.assertEqual(self.numbers(q='IR-2026'), {self.lesson_row.number})
        self.assertEqual(self.numbers(q='נועה'), {self.original_row.number})
        self.assertEqual(self.numbers(date_from='2026-08-10', date_to='2026-08-20'), {self.sale_row.number})
        self.assertEqual(self.numbers(purpose='archive', date_to='2026-08-09'), {self.lesson_row.number})

    def test_an_archive_copy_is_never_on_the_hand_delivery_or_held_lists(self):
        self.assertEqual(self.numbers(delivery='paper', printed='false'), {self.original_row.number})
        self.assertEqual(self.numbers(delivery='held'), set())
        self.assertEqual(self.client.get(STATUS).json()['counts']['paper_pending'], 1)

    def test_an_unknown_filter_value_is_400(self):
        for params in ({'purpose': 'copy'}, {'kind': 'x'}, {'date_from': '2026-13-01'}, {'date_to': 'yesterday'},
                       {'delivery': 'fax'}):
            self.assertEqual(self.client.get(ORIGINALS, params).status_code, 400, params)

    def test_a_row_written_by_the_previous_deployment_is_an_original(self):
        # The previous code inserts without the column: NULL, read as an original.
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO signed_originals (id, number, kind, source_id, channel, sha256, size, key_id, "
                "cert_fingerprint, sign_attempts, delivery, delivery_reason, document_type_label, customer_name, "
                "email_to, send_attempts, last_error, created_at, updated_at) VALUES (%s, 'RC-2026-000900', "
                "'formal', 'legacy', '', '', 0, '', '', 0, 'held', '', '', '', '', 0, '', now(), now())",
                [uuid.uuid4()],
            )
        row = SignedOriginal.objects.get(number='RC-2026-000900')
        self.assertIsNone(row.purpose)
        self.assertFalse(row.is_archive_copy)
        self.assertIn('RC-2026-000900', self.numbers(purpose='original'))
        self.assertNotIn('RC-2026-000900', self.numbers(purpose='archive'))
        listed = {r['number']: r for r in self.client.get(ORIGINALS).json()['results']}
        self.assertEqual(listed['RC-2026-000900']['purpose'], 'original')

    def test_print_original_refuses_an_archive_copy(self):
        response = self.client.post(print_url(self.lesson_row))
        self.assertEqual((response.status_code, response.json()), (409, {'error': ARCHIVE_NOT_ORIGINAL}))
        self.assertIsNone(SignedOriginal.objects.get(pk=self.lesson_row.pk).paper_original_printed_at)


@archive_on()
class FileDownloadTests(ArchiveApiMixin, APITestCase):
    def test_the_exact_stored_bytes_with_their_hash_and_a_log_line(self):
        row = sign_archive(IR, self.lesson_receipt('IR-2026-000001'))
        response = self.client.get(file_url(row), REMOTE_ADDR='10.1.2.3')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(response['Content-Disposition'], 'attachment; filename="IR-2026-000001.pdf"')
        self.assertEqual(response.content, bytes(row.pdf))
        self.assertEqual(response['X-Content-SHA256'], hashlib.sha256(response.content).hexdigest())
        self.assertEqual(response['X-Content-SHA256'], row.sha256)
        access = SignedFileAccess.objects.get()
        self.assertEqual((access.original_id, access.user, access.action, access.ip),
                         (row.pk, self.manager, 'download', '10.1.2.3'))
        # Downloading is not the original's one print, and changes nothing on the row.
        after = SignedOriginal.objects.get(pk=row.pk)
        self.assertEqual((after.paper_original_printed_at, after.delivery, after.sent_at),
                         (None, SignedOriginal.DELIVERY_NONE, None))

    def test_an_original_can_be_downloaded_too_without_using_its_one_print(self):
        with signing_on():
            doc = self.receipt('מזומן')
        row = SignedOriginal.objects.get(number=doc.document_number)
        response = self.client.get(file_url(row))
        self.assertEqual(response.content, bytes(row.pdf))
        self.assertIsNone(SignedOriginal.objects.get(pk=row.pk).paper_original_printed_at)

    def test_no_signed_bytes_is_404_and_nothing_is_logged(self):
        unsigned = SignedOriginal.objects.create(number='RC-2026-000777', kind=FORMAL, source_id='x')
        response = self.client.get(file_url(unsigned))
        self.assertEqual((response.status_code, response.json()), (404, {'error': FILE_NOT_FOUND}))
        self.assertEqual(self.client.get(f'{ORIGINALS}{uuid.uuid4()}/file/').status_code, 404)
        self.assertFalse(SignedFileAccess.objects.exists())

    def test_bytes_that_no_longer_match_their_hash_are_not_handed_out(self):
        row = sign_archive(IR, self.lesson_receipt('IR-2026-000001'))
        with connection.cursor() as cursor:
            cursor.execute('UPDATE signed_originals SET pdf = %s WHERE id = %s', [b'%PDF-broken', row.pk])
        self.assertEqual(self.client.get(file_url(row)).status_code, 500)
        self.assertFalse(SignedFileAccess.objects.exists())


@archive_on()
class ExportTests(ArchiveApiMixin, APITestCase):
    def setUp(self):
        super().setUp()
        for n in range(1, 4):
            self.lesson_receipt(f'IR-2026-00000{n}', day=n)
        self.rows = [sign_archive(IR, invoice) for invoice in Invoice.objects.order_by('invoice_number')]
        # Unsigned: never exported.
        SignedOriginal.objects.create(number='RC-2026-000777', kind=FORMAL, source_id='x')

    def export(self, **params):
        response = self.client.get(EXPORT, params)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response['Content-Type'], 'application/zip')
        return response, zipfile.ZipFile(io.BytesIO(response.content))

    def test_the_files_and_the_manifest(self):
        response, archive = self.export()
        self.assertEqual(sorted(archive.namelist()), [
            'IR-2026-000001.pdf', 'IR-2026-000002.pdf', 'IR-2026-000003.pdf', 'manifest.csv',
        ])
        for row in self.rows:
            data = archive.read(f'{row.number}.pdf')
            self.assertEqual(data, bytes(row.pdf))
            self.assertEqual(hashlib.sha256(data).hexdigest(), row.sha256)
        manifest = archive.read('manifest.csv').decode('utf-8-sig')
        lines = list(csv.reader(io.StringIO(manifest)))
        self.assertEqual(lines[0], ['number', 'purpose', 'kind', 'document_type_label', 'customer_name',
                                    'document_date', 'total', 'sha256', 'signed_at'])
        first = lines[1]
        self.assertEqual(first[:4], ['IR-2026-000001', 'archive', 'ir', self.rows[0].document_type_label])
        self.assertEqual((first[5], first[6], first[7]), ('2026-08-01', '236.00', self.rows[0].sha256))
        self.assertEqual((response['X-Export-Total'], response['X-Export-Next-Offset']), ('3', ''))
        accesses = SignedFileAccess.objects.filter(action='export')
        self.assertEqual(accesses.count(), 3)
        self.assertEqual({a.user for a in accesses}, {self.manager})

    def test_pages(self):
        first, archive = self.export(limit=2)
        self.assertEqual(sorted(archive.namelist()), ['IR-2026-000001.pdf', 'IR-2026-000002.pdf', 'manifest.csv'])
        self.assertEqual((first['X-Export-Total'], first['X-Export-Next-Offset']), ('3', '2'))
        second, archive = self.export(limit=2, offset=2)
        self.assertEqual(sorted(archive.namelist()), ['IR-2026-000003.pdf', 'manifest.csv'])
        self.assertEqual(second['X-Export-Next-Offset'], '')

    def test_the_page_is_capped_and_the_filters_apply(self):
        with patch('apps.documents.signing.views.MAX_EXPORT', 1):
            response, archive = self.export(limit=40)
        self.assertEqual(len(archive.namelist()), 2)
        self.assertEqual(response['X-Export-Next-Offset'], '1')
        _response, archive = self.export(q='000002')
        self.assertEqual(sorted(archive.namelist()), ['IR-2026-000002.pdf', 'manifest.csv'])
        _response, archive = self.export(purpose='original')
        self.assertEqual(archive.namelist(), ['manifest.csv'])
        self.assertEqual(self.client.get(EXPORT, {'kind': 'nope'}).status_code, 400)


class ArchivePermissionsTests(ArchiveFixture, APITestCase):
    @archive_on()
    def test_every_new_endpoint_is_for_managers(self):
        partner = make_user('partner-archive@test', UserProfile.ROLE_PARTNER)
        row = SignedOriginal.objects.create(number='RC-2026-000777', kind=FORMAL, source_id='x')
        for method, url in (('get', ARCHIVE_STATUS), ('post', ARCHIVE_RUN), ('get', file_url(row)),
                            ('get', EXPORT), ('get', ORIGINALS)):
            self.client.force_authenticate(None)
            self.assertEqual(getattr(self.client, method)(url).status_code, 401, url)
            self.client.force_authenticate(partner)
            self.assertEqual(getattr(self.client, method)(url).status_code, 403, url)
        self.assertFalse(SignedFileAccess.objects.exists())


class MigrationTests(TestCase):
    def test_0012_is_applied_and_its_tables_are_there(self):
        self.assertTrue(MigrationRecorder.Migration.objects.filter(
            app='documents', name='0012_signed_archive',
        ).exists())
        with connection.cursor() as cursor:
            columns = {
                column.name: column
                for column in connection.introspection.get_table_description(cursor, 'signed_originals')
            }
            self.assertIn('purpose', columns)
            self.assertTrue(columns['purpose'].null_ok)
            self.assertIn('signed_file_access', connection.introspection.table_names(cursor))
