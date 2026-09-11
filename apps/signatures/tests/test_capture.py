"""
record_registration_signature: what it stores, what it refuses, how one signing
for several children stays one row, and that it never raises. Plus the model's
refusal to be rewritten.
"""
import base64
import hashlib
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.core.models import RegistrationTerms
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Family
from apps.signatures.capture import (
    MAX_SIGNATURE_BYTES,
    PNG_MAGIC,
    merge_refs,
    record_registration_signature,
)
from apps.signatures.models import Signature, SignatureImmutableError
from apps.signatures.tests.helpers import TERMS_HTML, make_signature, png_bytes, png_data_url
from apps.signatures.text import html_to_paragraphs


def _payload(**overrides):
    data = {
        'parent_id_number': '123456782',
        'parent_first_name': 'רות',
        'parent_last_name': 'ניסן',
        'parent_phone': '0521234567',
        'parent_email': 'ruth@example.com',
        'signature': png_data_url(),
        'terms_consent': True,
        'health_consent': True,
    }
    data.update(overrides)
    return data


class CaptureTestBase(TestCase):
    def setUp(self):
        RegistrationTerms.objects.update_or_create(pk=1, defaults={'content': TERMS_HTML})
        self.branch = TestDataFactory.create_branch(name='צפון')
        self.family = TestDataFactory.create_family(branch=self.branch, parent_id_number='123456782')
        self.child = TestDataFactory.create_child(family=self.family, first_name='נועה')
        self.sibling = TestDataFactory.create_child(family=self.family, first_name='איתי')
        self.factory = RequestFactory()

    def _request(self, **meta):
        return self.factory.post('/api/v1/customers/widget/register/', **meta)

    def _capture(self, data=None, *, child=None, branch=None, refs=None, **meta):
        return record_registration_signature(
            self._request(**meta),
            family=self.family,
            child=child or self.child,
            branch=branch,
            data=data if data is not None else _payload(),
            refs=refs if refs is not None else {'payment_ids': ['pay-1'], 'trial': False},
        )


class RecordRegistrationSignatureTests(CaptureTestBase):
    def test_stores_the_signing_with_a_snapshot_of_the_terms(self):
        signature = self._capture(
            HTTP_X_FORWARDED_FOR='203.0.113.9, 10.0.0.1',
            HTTP_USER_AGENT='Mozilla/5.0 (Widget)',
            REMOTE_ADDR='10.9.9.9',
        )

        self.assertIsNotNone(signature)
        signature.refresh_from_db()
        self.assertEqual(signature.kind, Signature.KIND_REGISTRATION_TERMS)
        self.assertEqual(signature.document_title, 'תקנון הרשמה')
        self.assertEqual(signature.document_html, TERMS_HTML)
        self.assertEqual(signature.document_sha256, hashlib.sha256(TERMS_HTML.encode('utf-8')).hexdigest())
        self.assertEqual(bytes(signature.signature_png), png_bytes())
        self.assertEqual(signature.signature_sha256, hashlib.sha256(png_bytes()).hexdigest())
        self.assertEqual(signature.consents, {'health': True, 'terms': True, 'computerized_documents': True})
        self.assertEqual(signature.signer_name, 'רות ניסן')
        self.assertEqual(signature.signer_id_number, '123456782')
        self.assertEqual(signature.signer_phone, '0521234567')
        self.assertEqual(signature.signer_email, 'ruth@example.com')
        self.assertEqual(signature.ip_address, '203.0.113.9')
        self.assertEqual(signature.user_agent, 'Mozilla/5.0 (Widget)')
        self.assertEqual(signature.source, Signature.SOURCE_WIDGET)
        self.assertEqual(signature.family, self.family)
        self.assertEqual(list(signature.children.all()), [self.child])
        self.assertEqual(signature.refs, {'payment_ids': ['pay-1'], 'trial': False})
        # No registration branch given: the family's.
        self.assertEqual(signature.branch, self.branch)

    def test_the_snapshot_does_not_follow_later_edits_of_the_terms(self):
        signature = self._capture()
        RegistrationTerms.objects.filter(pk=1).update(content='<p>נוסח חדש</p>')

        signature.refresh_from_db()
        self.assertEqual(signature.document_html, TERMS_HTML)

    def test_the_registration_branch_wins_over_the_familys(self):
        other = TestDataFactory.create_branch(name='דרום')
        self.assertEqual(self._capture(branch=other).branch, other)

    def test_remote_addr_without_a_forwarded_header(self):
        self.assertEqual(self._capture(REMOTE_ADDR='198.51.100.4').ip_address, '198.51.100.4')

    def test_a_forwarded_value_that_is_not_an_ip_falls_back(self):
        signature = self._capture(HTTP_X_FORWARDED_FOR='unknown', REMOTE_ADDR='198.51.100.4')
        self.assertEqual(signature.ip_address, '198.51.100.4')

    def test_consents_are_what_was_sent(self):
        signature = self._capture(_payload(terms_consent='false', health_consent=False))
        self.assertEqual(signature.consents, {'health': False, 'terms': False, 'computerized_documents': False})

    def test_an_older_widget_build_says_terms_through_computerized_docs_consent(self):
        data = _payload(computerized_docs_consent=True)
        del data['terms_consent']
        del data['health_consent']
        signature = self._capture(data)
        self.assertEqual(signature.consents, {'health': False, 'terms': True, 'computerized_documents': True})


class RefusedSignatureTests(CaptureTestBase):
    def _assert_nothing_stored(self, data):
        self.assertIsNone(self._capture(data))
        self.assertFalse(Signature.objects.exists())

    def test_no_signature(self):
        data = _payload()
        del data['signature']
        self._assert_nothing_stored(data)

    def test_empty_signature(self):
        self._assert_nothing_stored(_payload(signature=''))

    def test_not_a_png_data_url(self):
        jpeg = 'data:image/jpeg;base64,' + base64.b64encode(b'\xff\xd8\xff\xe0' + b'0' * 100).decode()
        self._assert_nothing_stored(_payload(signature=jpeg))

    def test_png_prefix_on_bytes_that_are_not_a_png(self):
        fake = 'data:image/png;base64,' + base64.b64encode(b'GIF89a' + b'0' * 100).decode()
        self._assert_nothing_stored(_payload(signature=fake))

    def test_not_base64(self):
        self._assert_nothing_stored(_payload(signature='data:image/png;base64,@@not-base64@@'))

    def test_larger_than_the_limit(self):
        big = PNG_MAGIC + b'\0' * (MAX_SIGNATURE_BYTES + 1 - len(PNG_MAGIC))
        self._assert_nothing_stored(_payload(signature='data:image/png;base64,' + base64.b64encode(big).decode()))

    def test_exactly_at_the_limit_is_kept(self):
        at_limit = PNG_MAGIC + b'\0' * (MAX_SIGNATURE_BYTES - len(PNG_MAGIC))
        data = _payload(signature='data:image/png;base64,' + base64.b64encode(at_limit).decode())
        self.assertIsNotNone(self._capture(data))

    def test_not_a_string(self):
        self._assert_nothing_stored(_payload(signature={'image': 'x'}))


class SameSigningTests(CaptureTestBase):
    def test_two_children_with_one_signature_are_one_signing(self):
        first = self._capture(refs={'payment_ids': ['pay-1'], 'lesson_ids': ['l-1'], 'trial': False})
        second = self._capture(
            child=self.sibling, refs={'payment_ids': ['pay-2'], 'lesson_ids': ['l-1'], 'trial': False},
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Signature.objects.count(), 1)
        signature = Signature.objects.get()
        self.assertEqual(set(signature.children.all()), {self.child, self.sibling})
        self.assertEqual(signature.refs, {'payment_ids': ['pay-1', 'pay-2'], 'lesson_ids': ['l-1'], 'trial': False})

    def test_a_second_lesson_for_the_same_child_adds_its_refs(self):
        self._capture(refs={'payment_ids': ['pay-1']})
        self._capture(refs={'payment_ids': ['pay-2']})

        signature = Signature.objects.get()
        self.assertEqual(list(signature.children.all()), [self.child])
        self.assertEqual(signature.refs['payment_ids'], ['pay-1', 'pay-2'])

    def test_a_different_signature_is_another_signing(self):
        self._capture()
        self._capture(_payload(signature=png_data_url(seed=7)), child=self.sibling)
        self.assertEqual(Signature.objects.count(), 2)

    def test_the_same_signature_after_the_window_is_another_signing(self):
        first = self._capture()
        Signature.objects.filter(pk=first.pk).update(signed_at=timezone.now() - timedelta(minutes=31))
        self._capture(child=self.sibling)
        self.assertEqual(Signature.objects.count(), 2)

    def test_another_family_with_the_same_image_is_another_signing(self):
        self._capture()
        other_family = TestDataFactory.create_family(parent_id_number='987654321')
        record_registration_signature(
            self._request(), family=other_family,
            child=TestDataFactory.create_child(family=other_family), branch=None,
            data=_payload(), refs={},
        )
        self.assertEqual(Signature.objects.count(), 2)

    def test_merge_refs(self):
        self.assertEqual(
            merge_refs({'a': ['1'], 'trial': False, 'x': 'keep'}, {'a': ['1', '2'], 'trial': True, 'x': 'new', 'b': ['3']}),
            {'a': ['1', '2'], 'trial': True, 'x': 'keep', 'b': ['3']},
        )


class CaptureNeverRaisesTests(CaptureTestBase):
    def test_an_unexpected_error_is_logged_and_nothing_is_stored(self):
        with patch('apps.signatures.capture.get_registration_terms', side_effect=RuntimeError('boom')):
            with self.assertLogs('apps.signatures.capture', level='ERROR'):
                self.assertIsNone(self._capture())
        self.assertFalse(Signature.objects.exists())

    def test_a_database_error_rolls_back_only_the_capture(self):
        # The test runs inside a transaction, like a registration would if one
        # were open around the capture. The failed statement must leave it usable.
        def failing_terms():
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1/0')

        with patch('apps.signatures.capture.get_registration_terms', side_effect=failing_terms):
            with self.assertLogs('apps.signatures.capture', level='ERROR'):
                self.assertIsNone(self._capture())

        self.assertTrue(Family.objects.filter(pk=self.family.pk).exists())
        self.assertFalse(Signature.objects.exists())


class SignatureImmutabilityTests(TestCase):
    def setUp(self):
        self.signature = make_signature()

    def test_what_was_signed_cannot_be_changed(self):
        self.signature.signer_name = 'מישהו אחר'
        with self.assertRaises(SignatureImmutableError):
            self.signature.save()
        self.assertEqual(Signature.objects.get().signer_name, 'רות ניסן')

    def test_saving_unchanged_without_update_fields_is_refused_too(self):
        with self.assertRaises(SignatureImmutableError):
            self.signature.save()

    def test_refs_may_be_appended(self):
        self.signature.refs = {'payment_ids': ['p-1', 'p-2'], 'trial': False}
        self.signature.save(update_fields=['refs'])
        self.assertEqual(Signature.objects.get().refs['payment_ids'], ['p-1', 'p-2'])

    def test_refs_cannot_carry_another_field_with_them(self):
        self.signature.document_html = '<p>אחר</p>'
        with self.assertRaises(SignatureImmutableError):
            self.signature.save(update_fields=['refs', 'document_html'])
        self.assertEqual(Signature.objects.get().document_html, TERMS_HTML)

    def test_deleting_the_family_keeps_the_signature(self):
        family = TestDataFactory.create_family()
        signature = make_signature(family=family, seed=3)
        family.delete()
        signature.refresh_from_db()
        self.assertIsNone(signature.family_id)
        self.assertEqual(signature.signer_name, 'רות ניסן')


class HtmlToParagraphsTests(TestCase):
    def test_blocks_become_paragraphs(self):
        self.assertEqual(
            html_to_paragraphs(TERMS_HTML),
            [
                '1. מחיר שנתי: המחיר הינו מחיר שנתי & חודשי.',
                '• סעיף ברשימה',
                'מסמכים ממוחשבים: ההורה מסכים לקבל חשבוניות בדוא"ל.',
            ],
        )

    def test_scripts_and_empty_blocks_are_dropped(self):
        self.assertEqual(
            html_to_paragraphs('<p> </p><script>alert(1)</script><div>א<br>ב</div>'),
            ['א', 'ב'],
        )
