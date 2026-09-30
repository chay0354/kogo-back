"""
The credit note form's "נותר לזכות" (audit M4, #27): GET documents/credit-room/
answers what is left of an original to credit, before VAT, by the rule the
credit note itself is checked by — and the customer's confirmation recorded
with the day it arrived (הוראה 23א(3)).
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.core.tranzila_ledger import list_ledger_documents
from apps.customers.financial_models import Invoice
from apps.documents.models import FormalDocument
from apps.documents.tests.test_document_dates import Fixture
from apps.documents.tests.test_drafts_and_pdf import make_user
from apps.rentals.tests.factories import make_branch
from apps.rentals.tests.factories import make_user as make_scoped_user

ROOM = '/api/v1/documents/documents/credit-room/'
LIST = '/api/v1/documents/documents/'


@override_settings(TRANZILA_BILLING_TERMINAL='')
@patch('apps.documents.service._email_credit_note')
class CreditRoomTests(Fixture, APITestCase):
    def original(self, kind='tax_invoice', number=None, net='200.00'):
        net = Decimal(net)
        return FormalDocument.objects.create(
            document_number=number or f'TI-{self.year}-000900', document_type=kind, client_type='existing',
            child=self.kid, document_date=self.today, subtotal=net, vat_amount=net * Decimal('0.18'),
            total_amount=net * Decimal('1.18'),
        )

    def credit(self, number, amount='100.00'):
        return self.client.post('/api/v1/documents/documents/create-document/', {
            'document_type': 'credit_invoice',
            'client_type': 'existing',
            'child_id': str(self.kid.id),
            'credit_invoice_details': {
                'document_date': str(self.today), 'linked_invoice_id': number,
                'credit_reason': 'ביטול', 'credit_amount_before_vat': amount,
            },
        }, format='json')

    def setUp(self):
        super().setUp()
        self.manager = make_user('room-manager@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)

    def room(self, number):
        res = self.client.get(ROOM, {'number': number})
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def test_a_tax_invoice_has_all_of_itself_left_until_it_is_credited(self, _mail):
        original = self.original(net='200.00')
        room = self.room(original.document_number)
        self.assertEqual(
            (room['known'], room['creditable'], room['kind'], room['net'], room['credited'], room['left']),
            (True, True, 'formal', '200.00', '0.00', '200.00'),
        )
        self.assertEqual(room['document_date'], original.document_date.isoformat())
        self.assertEqual(room['child_id'], str(self.kid.id))

        self.assertEqual(self.credit(original.document_number, '150.00').status_code, 201)
        room = self.room(original.document_number)
        self.assertEqual((room['credited'], room['left']), ('150.00', '50.00'))

    def test_the_room_is_what_the_credit_note_rule_allows(self, _mail):
        original = self.original(net='200.00')
        self.credit(original.document_number, '120.00')
        left = Decimal(self.room(original.document_number)['left'])
        self.assertEqual(self.credit(original.document_number, str(left + Decimal('0.01'))).status_code, 400)
        self.assertEqual(self.credit(original.document_number, str(left)).status_code, 201)
        self.assertEqual(self.room(original.document_number)['left'], '0.00')

    def test_an_invoice_receipt_is_creditable(self, _mail):
        original = self.original('combined', f'IRM-{self.year}-000900', net='100.00')
        room = self.room(original.document_number)
        self.assertEqual((room['creditable'], room['document_type'], room['left']), (True, 'combined', '100.00'))

    def test_a_receipt_says_why_it_cannot_be_credited(self, _mail):
        receipt = self.original('receipt', f'RC-{self.year}-000900')
        room = self.room(receipt.document_number)
        self.assertEqual((room['known'], room['creditable'], room['left']), (True, False, None))
        self.assertIn('חשבונית מס', room['refusal'])

    def test_a_lesson_receipt_is_measured_before_vat(self, _mail):
        family = self.kid.family
        Invoice.objects.create(
            invoice_number=f'IR-{self.year}-000900', family=family, amount=Decimal('236.00'), status='paid',
            payment_method='credit_card', payment_type='recurring', payer_name=family.name,
            invoice_date=timezone.now(),
        )
        room = self.room(f'IR-{self.year}-000900')
        self.assertEqual((room['kind'], room['net'], room['left']), ('lesson', '200.00', '200.00'))
        self.assertIsNotNone(room['document_date'])

    def test_a_number_kogo_never_issued_is_unknown(self, _mail):
        room = self.room('30112')
        self.assertEqual((room['known'], room['creditable'], room['net'], room['left']), (False, False, None, None))

    def test_a_number_is_required(self, _mail):
        self.assertEqual(self.client.get(ROOM).status_code, 400)

    def test_a_partner_asks_about_their_own_branches_only(self, _mail):
        mine, elsewhere = make_branch('שלי'), make_branch('רחוק')
        theirs = self.original(number=f'TI-{self.year}-000901')
        FormalDocument.objects.filter(pk=theirs.pk).update(branch=elsewhere)
        own = self.original(number=f'TI-{self.year}-000902')
        FormalDocument.objects.filter(pk=own.pk).update(branch=mine)
        self.client.force_authenticate(make_scoped_user('room-partner@test', UserProfile.ROLE_PARTNER, [mine]))
        self.assertEqual(self.client.get(ROOM, {'number': theirs.document_number}).status_code, 403)
        self.assertEqual(self.client.get(ROOM, {'number': own.document_number}).status_code, 200)


@override_settings(TRANZILA_BILLING_TERMINAL='')
class AckWithADateTests(Fixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.manager = make_user('ack-date-manager@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)
        if self.today.day == 1:
            self.skipTest('no earlier day in this month to date the credit note on')
        self.note = self.issued(f'CR-{self.year}-000900', 'credit_invoice', self.today - timedelta(days=1))

    def ack(self, **body):
        return self.client.post(f'{LIST}{self.note.pk}/customer-ack/', {'note': 'חתימה על העתק', **body}, format='json')

    def test_a_confirmation_that_arrived_earlier_is_kept_on_its_day(self):
        day = self.note.document_date
        res = self.ack(date=day.isoformat())
        self.assertEqual(res.status_code, 200, res.data)
        self.note.refresh_from_db()
        self.assertEqual(self.note.customer_ack_at.astimezone(ZoneInfo('Asia/Jerusalem')).date(), day)

    def test_todays_confirmation_is_stamped_now(self):
        before = timezone.now()
        self.assertEqual(self.ack(date=self.today.isoformat()).status_code, 200)
        self.note.refresh_from_db()
        self.assertGreaterEqual(self.note.customer_ack_at, before)

    def test_a_day_in_the_future_or_before_the_credit_note_is_refused(self):
        for day in (self.today + timedelta(days=1), self.note.document_date - timedelta(days=1)):
            res = self.ack(date=day.isoformat())
            self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(self.ack(date='31/12/2026').status_code, 400)
        self.note.refresh_from_db()
        self.assertIsNone(self.note.customer_ack_at)

    def test_the_ledger_row_says_whether_the_customer_confirmed(self):
        def row():
            rows = list_ledger_documents(
                start_date=self.note.document_date, end_date=self.today, local_only=True,
            )['documents']
            return next(r for r in rows if r['id'] == str(self.note.pk))

        self.assertIsNone(row()['customer_ack_at'])
        self.ack()
        self.assertIsNotNone(row()['customer_ack_at'])
        self.assertEqual(row()['child_id'], str(self.kid.id))
