"""Standing-order card-update link: token, preview, charge, ManyChat fields."""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.manychat_service import ManyChatService
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.card_update import (
    CardUpdateError,
    apply_new_card,
    build_card_update_token,
    format_sto_amount,
    preview_payload,
    resolve_card_update_token,
    send_card_update_whatsapp,
)
from apps.customers.models import Payment, RecurringPayment


CARD = {
    'card_number': '4580458045804580',
    'expiry_month': 12,
    'expiry_year': 2028,
    'cvv': '123',
    'card_holder_id': '123456782',
}

TRANZILA_OK = {
    'success': True,
    'transaction_id': 'cu-1',
    'confirmation_code': 'AUTHCU',
    'token': 'Ynewtoken4580',
    'amount': 250.0,
    'response_code': '000',
    'raw_response': {'transaction_result': {'token': 'Ynewtoken4580'}},
}


def _failed_sto(*, amount='250.00', token='12'):
    family = TestDataFactory.create_family()
    TestDataFactory.create_parent(family=family)
    child = TestDataFactory.create_child(family=family)
    lesson = TestDataFactory.create_lesson()
    payment = Payment.objects.create(
        child=child,
        family=family,
        parent=family.parents.first(),
        lesson=lesson,
        branch=lesson.course.branch,
        payment_type='recurring_subscription',
        status='completed',
        base_amount=Decimal(amount),
        discount_amount=Decimal('0.00'),
        final_amount=Decimal(amount),
        registration_fee=Decimal('0.00'),
        description='מנוי',
    )
    recurring = RecurringPayment.objects.create(
        child=child,
        initial_payment=payment,
        tranzila_token=token,
        status='failed',
        base_amount=Decimal(amount),
        amount=Decimal(amount),
        billing_day=1,
        start_date=date(2026, 8, 1),
        next_billing_date=date(2026, 9, 1),
        card_expire_month=8,
        card_expire_year=2026,
    )
    return recurring


class CardUpdateTokenTests(TestCase):
    def test_round_trip_and_amount_label(self):
        recurring = _failed_sto(amount='250.00')
        token = build_card_update_token(recurring)
        self.assertNotIn(':', token)
        resolved, already_done = resolve_card_update_token(token)
        self.assertEqual(resolved.id, recurring.id)
        self.assertFalse(already_done)
        self.assertEqual(format_sto_amount(recurring.amount), '250')

    def test_rejects_garbage(self):
        with self.assertRaises(CardUpdateError):
            resolve_card_update_token('not-a-token')

    def test_stale_token_after_fix_is_already_done(self):
        recurring = _failed_sto()
        token = build_card_update_token(recurring)
        recurring.status = 'active'
        recurring.tranzila_token = 'Yfixed'
        recurring.save(update_fields=['status', 'tranzila_token', 'updated_at'])
        resolved, already_done = resolve_card_update_token(token)
        self.assertTrue(already_done)
        self.assertEqual(resolved.id, recurring.id)


@override_settings(
    CRM_FRONTEND_URL='https://kogo-front.vercel.app',
    TRANZILA_TERMINAL='test_terminal',
    TRANZILA_PUBLIC_KEY='test_public_key',
    TRANZILA_SECRET_KEY='test_secret_key',
    TRANZILA_PROD_TERMINAL='test_terminal',
    TRANZILA_PROD_TOKEN_TERMINAL='test_terminal',
    TRANZILA_PROD_PUBLIC_KEY='test_public_key',
    TRANZILA_PROD_SECRET_KEY='test_secret_key',
)
class CardUpdateApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.recurring = _failed_sto()
        self.token = build_card_update_token(self.recurring)

    def test_preview_is_public(self):
        res = self.client.get(f'/api/v1/customers/card-update/{self.token}/')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['child_name'], self.recurring.child.full_name)
        self.assertTrue(res.data['will_charge'])
        self.assertFalse(res.data['already_done'])

    @patch('apps.customers.card_update.PaymentService._create_invoice_from_payment')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_charge_replaces_token_and_reactivates(self, mock_charge, _invoice):
        res = self.client.post(
            f'/api/v1/customers/card-update/{self.token}/charge/',
            {'card_details': CARD},
            format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data['success'])
        self.assertTrue(res.data['charged'])
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.status, 'active')
        self.assertEqual(self.recurring.tranzila_token, 'Ynewtoken4580')
        self.assertEqual(self.recurring.card_expire_month, 12)
        self.assertEqual(self.recurring.card_expire_year, 2028)
        self.assertEqual(self.recurring.next_billing_date, date(2026, 10, 1))
        mock_charge.assert_called_once()

    @patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=TRANZILA_OK)
    def test_skips_charge_when_month_already_paid(self, mock_verify):
        self.recurring.last_charge_date = date(2026, 9, 1)
        self.recurring.next_billing_date = date(2026, 10, 1)
        self.recurring.save(update_fields=['last_charge_date', 'next_billing_date', 'updated_at'])
        token = build_card_update_token(self.recurring)
        apply_new_card(self.recurring, CARD)
        mock_verify.assert_called_once()
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.tranzila_token, 'Ynewtoken4580')
        self.assertEqual(self.recurring.status, 'active')
        self.assertEqual(self.recurring.next_billing_date, date(2026, 10, 1))


class CardUpdateWhatsAppTests(TestCase):
    @override_settings(CRM_FRONTEND_URL='https://kogo-front.vercel.app')
    @patch(
        'apps.customers.card_update.ManyChatService.notify_registration',
        return_value={'sent': True, 'method': 'flow'},
    )
    def test_notify_includes_link_fields(self, mock_notify):
        recurring = _failed_sto(amount='350.00')
        result = send_card_update_whatsapp(recurring)
        self.assertTrue(result['sent'])
        kwargs = mock_notify.call_args.kwargs
        self.assertEqual(kwargs['kind'], ManyChatService.REGISTRATION_KIND_CARD_UPDATE)
        extra = kwargs['extra_fields']
        self.assertEqual(extra['kogo_amount'], '350')
        self.assertTrue(extra['kogo_card_update_url'].startswith('https://kogo-front.vercel.app/update-card/'))
        self.assertTrue(extra['kogo_card_update_token'])
        preview = preview_payload(recurring)
        self.assertEqual(preview['course_name'], recurring.initial_payment.lesson.course.name)
