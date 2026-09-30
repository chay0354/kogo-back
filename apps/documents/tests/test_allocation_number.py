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


# ── the gate: an original that needs a number is not signed without it ──────
#
# The Tax Authority's "חשבוניות ישראל" FAQ (question 10): the allocation number
# belongs on the original; a number obtained after the original was issued goes
# on a copy only. So a tax invoice to a business above the threshold is held,
# unsigned, until its number is entered — and entering it signs and mails it.

from datetime import date, timedelta  # noqa: E402
from unittest.mock import patch  # noqa: E402

from django.utils import timezone  # noqa: E402
from rest_framework.test import APITestCase  # noqa: E402

from apps.customers.models import BusinessCustomer  # noqa: E402
from apps.documents import service  # noqa: E402
from apps.documents.models import SignedOriginal  # noqa: E402
from apps.documents.signing.service import (  # noqa: E402
    ALLOCATION_COPY_ONLY, REASON_AWAITING_ALLOCATION, sign_pending,
)
from apps.documents.tests.signing_support import attachment_bytes, pdf_text, signing_on  # noqa: E402
from apps.documents.tests.test_register import RegisterFixture, make_user  # noqa: E402

NUMBER = '123456789'
ORIGINALS = '/api/v1/documents/signing/originals/'


def allocation_url(doc) -> str:
    return f'/api/v1/documents/documents/{doc.pk}/allocation-number/'


def aged(row, hours):
    SignedOriginal.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(hours=hours))


class GateMixin(RegisterFixture):
    def setUp(self):
        super().setUp()
        self.customer = BusinessCustomer.objects.create(first_name='סטודיו', last_name='אור', email='or@example.com')
        patcher = patch('apps.documents.document_email.send_resend_email', return_value='msg-id')
        self.mail = patcher.start()
        self.addCleanup(patcher.stop)

    def business_invoice(self, price=6000, document_type='tax_invoice', **payload):
        body = {
            'client_type': 'business', 'business_customer_id': str(self.customer.pk),
            'invoice_details': {
                'document_date': '2026-09-18',
                'line_items': [{'description': 'ייעוץ', 'quantity': 1, 'price': price}],
                **payload,
            },
        }
        with self.captureOnCommitCallbacks(execute=True):
            if document_type == 'combined':
                body['invoice_details']['payment_methods'] = ['העברה בנקאית']
                return service.create_combined(body)
            return service.create_invoice(body, document_type)

    def row(self, doc) -> SignedOriginal:
        return SignedOriginal.objects.get(number=doc.document_number)

    def enter(self, doc, value, user=None):
        self.client.force_authenticate(user or self.manager)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(allocation_url(doc), {'allocation_number': value}, format='json')


@signing_on()
class AllocationGateTests(GateMixin, APITestCase):
    def test_held_unsigned_and_unmailed_while_it_waits(self):
        doc = self.business_invoice()
        row = self.row(doc)
        self.assertFalse(row.is_signed)
        self.assertEqual((row.delivery, row.delivery_reason), ('held', REASON_AWAITING_ALLOCATION))
        self.assertEqual(row.channel, SignedOriginal.CHANNEL_FORMAL)
        self.mail.assert_not_called()

        # The cron does not sign it either, however often it runs.
        SignedOriginal.objects.update(created_at=timezone.now() - timedelta(minutes=10))
        for _ in range(2):
            summary = sign_pending()
            self.assertEqual(summary['signed'], 0)
        self.assertFalse(self.row(doc).is_signed)
        self.mail.assert_not_called()

        # The office sees it, and why.
        self.client.force_authenticate(self.manager)
        listed = self.client.get(ORIGINALS, {'delivery': 'held'}).json()['results']
        self.assertEqual([(r['number'], r['awaiting_allocation'], r['source_id']) for r in listed],
                         [(doc.document_number, True, str(doc.pk))])
        status = self.client.get('/api/v1/documents/signing/status/').json()
        self.assertEqual(status['counts']['awaiting_allocation'], 1)
        # Nor can it be sent or printed before it exists.
        self.assertEqual(self.client.post(f'{ORIGINALS}{row.pk}/send/', {}, format='json').status_code, 409)
        self.assertEqual(self.client.post(f'{ORIGINALS}{row.pk}/print-original/').status_code, 409)

    def test_the_first_number_signs_it_once_with_the_number_on_it_and_mails_it(self):
        doc = self.business_invoice()
        response = self.enter(doc, '123-456-789')
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body['allocation_number'], body['signed'], body['delivery'], body['copy_only']),
                         (NUMBER, True, 'email', False))
        row = self.row(doc)
        self.assertTrue(row.is_signed)
        self.assertIsNotNone(row.sent_at)
        self.assertIn(NUMBER, pdf_text(bytes(row.pdf)))
        self.mail.assert_called_once()
        self.assertEqual(self.mail.call_args.kwargs['to'], ['or@example.com'])
        self.assertEqual(attachment_bytes(self.mail), bytes(row.pdf))
        doc.refresh_from_db()
        self.assertEqual((doc.allocation_number, doc.allocation_entered_by), (NUMBER, self.manager))

        # The same number again changes nothing and sends nothing; the cron neither.
        again = self.enter(doc, NUMBER)
        self.assertEqual(again.status_code, 200)
        SignedOriginal.objects.update(created_at=timezone.now() - timedelta(minutes=10))
        sign_pending()
        self.mail.assert_called_once()
        self.assertEqual(self.row(doc).sha256, row.sha256)

    def test_once_signed_the_number_cannot_be_changed_or_cleared(self):
        doc = self.business_invoice()
        self.enter(doc, NUMBER)
        for value in ('987654321', ''):
            response = self.enter(doc, value)
            self.assertEqual(response.status_code, 409, value)
            self.assertIn(NUMBER, response.json()['error'])
            self.assertIn('העתק', response.json()['error'])
        doc.refresh_from_db()
        self.assertEqual(doc.allocation_number, NUMBER)
        self.mail.assert_called_once()

    def test_before_it_is_signed_the_number_may_still_be_cleared(self):
        doc = self.business_invoice()
        # A number entered and cleared inside one transaction never reaches the signing.
        FormalDocument.objects.filter(pk=doc.pk).update(allocation_number='')
        self.client.force_authenticate(self.manager)
        response = self.client.post(allocation_url(doc), {'allocation_number': ''}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.row(doc).delivery_reason, REASON_AWAITING_ALLOCATION)

    def test_a_number_that_arrives_another_way_is_signed_by_the_cron(self):
        doc = self.business_invoice()
        FormalDocument.objects.filter(pk=doc.pk).update(allocation_number=NUMBER)
        SignedOriginal.objects.update(created_at=timezone.now() - timedelta(minutes=10))
        summary = sign_pending()
        self.assertEqual((summary['signed'], summary['sent']), (1, 1))
        self.assertIn(NUMBER, pdf_text(bytes(self.row(doc).pdf)))
        self.mail.assert_called_once()

    def test_a_private_family_is_not_held(self):
        with self.captureOnCommitCallbacks(execute=True):
            doc = service.create_invoice({
                'client_type': 'existing', 'child_id': str(self.kid.id),
                'invoice_details': {'document_date': '2026-09-18',
                                    'line_items': [{'description': 'קייטנה', 'quantity': 1, 'price': 9000}]},
            }, 'tax_invoice')
        row = self.row(doc)
        self.assertTrue(row.is_signed)
        self.assertNotEqual(row.delivery_reason, REASON_AWAITING_ALLOCATION)

    def test_below_the_threshold_a_receipt_and_a_combined_document(self):
        small = self.business_invoice(price=5000)  # "עולה על" — exactly the threshold needs none
        self.assertTrue(self.row(small).is_signed)
        combined = self.business_invoice(document_type='combined')
        self.assertEqual(self.row(combined).delivery_reason, REASON_AWAITING_ALLOCATION)
        with self.captureOnCommitCallbacks(execute=True):
            receipt = service.create_receipt({
                'client_type': 'business', 'business_customer_id': str(self.customer.pk),
                'document_date': '2026-09-18',
                'receipt_details': {'payment_method': 'העברה בנקאית', 'bank_amount': '9000.00'},
            })
        self.assertTrue(self.row(receipt).is_signed)  # a receipt carries no allocation number

    def test_an_original_signed_before_the_gate_takes_a_number_for_its_copies_only(self):
        with patch('apps.documents.signing.sources.FormalDocumentSource.awaiting_allocation', return_value=False):
            doc = self.business_invoice()
        row = self.row(doc)
        self.assertTrue(row.is_signed)
        self.mail.assert_called_once()
        response = self.enter(doc, NUMBER)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()['copy_only'], response.json()['message']), (True, ALLOCATION_COPY_ONLY))
        after = self.row(doc)
        self.assertEqual(bytes(after.pdf), bytes(row.pdf))  # the original never changes
        self.assertNotIn(NUMBER, pdf_text(bytes(after.pdf)))
        self.mail.assert_called_once()
        # The office's copy carries it.
        copy = self.client.get(f'{ORIGINALS}{row.pk}/file/', {'copy': '1'})
        self.assertIn(NUMBER, pdf_text(copy.content))

    def test_signing_off_keeps_the_endpoint_as_before(self):
        with self.settings(DOCUMENT_SIGNING_ENABLED=False):
            doc = self.business_invoice()
            self.assertEqual(self.enter(doc, NUMBER).status_code, 200)
            self.assertEqual(self.enter(doc, '').status_code, 200)
        self.assertFalse(SignedOriginal.objects.exists())

    def test_a_worker_cannot_set_it(self):
        doc = self.business_invoice()
        response = self.enter(doc, NUMBER, user=make_user('worker-gate@test', UserProfile.ROLE_WORKER))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.row(doc).is_signed)


class DailyBriefAllocationTests(GateMixin, TestCase):
    @signing_on()
    def test_a_line_for_invoices_waiting_more_than_a_day(self):
        from apps.core.daily_brief import GREEN, YELLOW, check_awaiting_allocation

        self.assertEqual(check_awaiting_allocation(date(2026, 9, 25)).severity, GREEN)
        fresh = self.business_invoice()
        old = self.business_invoice()
        aged(self.row(old), 30)
        aged(self.row(fresh), 3)
        item = check_awaiting_allocation(date(2026, 9, 25))
        self.assertEqual((item.severity, item.count), (YELLOW, 1))
        self.assertIn(old.document_number, item.rows[0]['label'])
        self.assertIn('מספר הקצאה', item.title)


@signing_on(RENTAL_BILLING_ENABLED=True)
class RentalReceiptGateTests(APITestCase):
    """An RT rental receipt is a combined document to a business: above the threshold, it waits too."""

    def test_a_receipt_above_the_threshold_is_held_and_its_number_releases_it(self):
        from apps.rental_billing.billing import charge_due
        from apps.rental_billing.models import TenantCharge
        from apps.rental_billing.tests.factories import (
            make_branch, make_tenancy, make_user as rental_user, mocked_gateway, patch_manychat, patch_tranzila,
        )
        from apps.rental_billing.orders import open_standing_order

        gateway = mocked_gateway()
        patch_tranzila(self, gateway)
        patch_manychat(self)
        today = patch('apps.rental_billing.billing.today_local', return_value=date(2026, 9, 11))
        today.start()
        self.addCleanup(today.stop)
        manager = rental_user('manager-rt-gate@test', UserProfile.ROLE_MANAGER)
        tenancy = make_tenancy(make_branch('פלורנטין'), monthly_amount=Decimal('6000.00'))
        order = open_standing_order(tenancy, user=manager)
        from apps.rental_billing.models import TenantStandingOrder

        TenantStandingOrder.objects.filter(pk=order.pk).update(
            status=TenantStandingOrder.STATUS_ACTIVE, tranzila_token='tok_saved', card_expire_month=12,
            card_expire_year=2030, card_last4='4242', next_charge_date=date(2026, 10, 10),
        )
        # The factory makes any real rental mail fail the test: the held receipt sends nothing.
        with self.captureOnCommitCallbacks(execute=True):
            charge_due(today=date(2026, 10, 10))
        charge = TenantCharge.objects.get()
        self.assertEqual(charge.status, TenantCharge.STATUS_CHARGED)
        doc = charge.receipt
        self.assertEqual((doc.document_type, doc.client_type, doc.subtotal), ('combined', 'business', Decimal('6000.00')))
        row = SignedOriginal.objects.get(number=doc.document_number)
        self.assertEqual((row.channel, row.delivery, row.delivery_reason),
                         (SignedOriginal.CHANNEL_RENTAL, 'held', REASON_AWAITING_ALLOCATION))
        self.assertFalse(row.is_signed)
        self.assertIsNone(TenantCharge.objects.get().receipt_emailed_at)

        self.client.force_authenticate(manager)
        with patch('apps.rental_billing.receipt_email.send_resend_email', return_value='msg-id') as resend, \
                self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(allocation_url(doc), {'allocation_number': NUMBER}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        row.refresh_from_db()
        self.assertTrue(row.is_signed)
        self.assertIn(NUMBER, pdf_text(bytes(row.pdf)))
        resend.assert_called_once()
        self.assertEqual(attachment_bytes(resend), bytes(row.pdf))
        self.assertIsNotNone(TenantCharge.objects.get().receipt_emailed_at)
