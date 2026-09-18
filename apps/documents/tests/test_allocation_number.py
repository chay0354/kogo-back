"""מספר הקצאה typed in by hand: when it is needed, what it accepts, where it shows."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.documents.document_pdf import _notes
from apps.documents.invoice_document import allocation_note, allocation_required
from apps.documents.models import FormalDocument

User = get_user_model()


def _doc(subtotal='6000', discount='0', doc_type='tax_invoice', allocation=''):
    return FormalDocument.objects.create(
        document_number=f'TEST-{FormalDocument.objects.count() + 1}',
        document_type=doc_type,
        client_type='business',
        document_date='2026-09-14',
        subtotal=Decimal(subtotal),
        discount_amount=Decimal(discount),
        vat_percent=Decimal('18'),
        vat_amount=Decimal('0'),
        total_amount=Decimal(subtotal),
        allocation_number=allocation,
    )


class ThresholdTests(TestCase):
    def test_above_the_threshold_needs_one(self):
        # סעיף 38(א1) לחוק מע"מ: "עולה על" — the threshold itself needs none.
        self.assertFalse(allocation_required(Decimal('5000')))
        self.assertTrue(allocation_required(Decimal('5000.01')))

    def test_below_does_not(self):
        self.assertFalse(allocation_required(Decimal('4999.99')))
        self.assertFalse(allocation_required(Decimal('0')))

    def test_the_threshold_is_a_setting_not_a_constant(self):
        """It steps down year by year and is printed on real invoices."""
        import importlib

        from apps.documents import invoice_document

        try:
            with override_settings(ALLOCATION_THRESHOLD_ILS='15000'):
                importlib.reload(invoice_document)
                self.assertFalse(invoice_document.allocation_required(Decimal('6000')))
                self.assertTrue(invoice_document.allocation_required(Decimal('15000.01')))
                self.assertIn('15,000', invoice_document.allocation_note(Decimal('100')).text)
        finally:
            # Reloaded once the override is gone: reloading inside it left the
            # ₪15,000 threshold in place for every test that ran after this one.
            importlib.reload(invoice_document)


class NoteTests(TestCase):
    def test_the_number_itself_is_printed_once_entered(self):
        note = allocation_note(Decimal('9000'), '123456789')
        self.assertEqual(note.text, '123456789')

    def test_missing_above_the_threshold_says_so(self):
        self.assertIn('טרם הוזן', allocation_note(Decimal('9000'), '').text)

    def test_below_the_threshold_says_none_is_needed(self):
        self.assertIn('לא נדרש', allocation_note(Decimal('100'), '').text)

    def test_a_number_below_the_threshold_is_still_printed(self):
        """Any invoice may carry one — the threshold only says when it is required."""
        self.assertEqual(allocation_note(Decimal('100'), '123456789').text, '123456789')

    def test_the_document_notes_carry_it(self):
        doc = _doc(subtotal='9000', allocation='987654321')
        values = [n.text for n in _notes(doc)]
        self.assertIn('987654321', values)


class ApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        user = User.objects.create_user(
            username='mgr-alloc@x.com', email='mgr-alloc@x.com', password='pass12345!', is_active=True,
        )
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        token = Token.objects.create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        self.user = user
        self.doc = _doc(subtotal='9000')

    def _post(self, value, doc=None):
        doc = doc or self.doc
        return self.client.post(
            f'/api/v1/documents/documents/{doc.id}/allocation-number/',
            {'allocation_number': value}, format='json',
        )

    def test_nine_digits_are_stored_with_who_and_when(self):
        res = self._post('123456789')
        self.assertEqual(res.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.allocation_number, '123456789')
        self.assertIsNotNone(self.doc.allocation_entered_at)
        self.assertEqual(self.doc.allocation_entered_by, self.user)

    def test_spaces_and_dashes_are_accepted(self):
        self._post('123-456 789')
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.allocation_number, '123456789')

    def test_a_wrong_length_is_refused(self):
        for bad in ('1234', '1234567890'):
            res = self._post(bad)
            self.assertEqual(res.status_code, 400, bad)
            self.assertIn('9 ספרות', res.data['error'])
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.allocation_number, '')

    def test_it_can_be_cleared(self):
        self._post('123456789')
        res = self._post('')
        self.assertEqual(res.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.allocation_number, '')
        self.assertIsNone(self.doc.allocation_entered_at)

    def test_not_on_a_document_that_cannot_carry_one(self):
        draft = _doc(subtotal='9000', doc_type='draft')
        res = self._post('123456789', doc=draft)
        self.assertEqual(res.status_code, 400)

    def test_the_list_says_which_rows_need_one(self):
        _doc(subtotal='100')
        res = self.client.get('/api/v1/documents/documents/')
        rows = res.data.get('results', res.data)
        needed = {r['document_number']: r['allocation_required'] for r in rows}
        self.assertTrue(needed[self.doc.document_number])
        self.assertIn(False, needed.values())

    def test_a_worker_cannot_set_it(self):
        worker = User.objects.create_user(username='w-alloc@x.com', email='w-alloc@x.com',
                                          password='pass12345!', is_active=True)
        UserProfile.objects.update_or_create(user=worker, defaults={'role': UserProfile.ROLE_WORKER})
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=worker).key}')
        res = c.post(f'/api/v1/documents/documents/{self.doc.id}/allocation-number/',
                     {'allocation_number': '123456789'}, format='json')
        self.assertEqual(res.status_code, 403)
