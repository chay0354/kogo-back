"""Card replacement endpoints: who may call them, and what each side is told."""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.card_replacement import build_family_token
from apps.customers.models import Payment, RecurringPayment

CARD = {
    'card_number': '4580458045804580',
    'expiry_month': 12,
    'expiry_year': 2030,
    'cvv': '123',
    'card_holder_id': '123456782',
}
VERIFY_OK = {'success': True, 'token': 'Ynew4580', 'response_code': '000',
             'raw_response': {'transaction_result': {'token': 'Ynew4580'}}}
CHARGE_OK = {'success': True, 'transaction_id': 'x', 'confirmation_code': 'A',
             'response_code': '000', 'raw_response': {}}


def _family_with_failed_sto():
    family = TestDataFactory.create_family()
    TestDataFactory.create_parent(family=family)
    child = TestDataFactory.create_child(family=family)
    lesson = TestDataFactory.create_lesson()
    initial = Payment.objects.create(
        child=child, family=family, parent=family.parents.first(), lesson=lesson,
        branch=lesson.course.branch, payment_type='recurring_subscription',
        status='completed', base_amount=Decimal('240'), final_amount=Decimal('240'),
        registration_fee=Decimal('0.00'), description='מנוי',
    )
    RecurringPayment.objects.create(
        child=child, initial_payment=initial, tranzila_token='OLD', status='failed',
        base_amount=Decimal('240'), amount=Decimal('240'), billing_day=1,
        start_date=date(2026, 8, 1), next_billing_date=date(2026, 9, 1),
        card_expire_month=8, card_expire_year=2026,
    )
    return family


def _login(client, role, username):
    """Token auth, like the other API tests here.

    `force_authenticate` hands the view a User object whose cached `.profile`
    still holds the role a signal gave it at creation, so a manager arrives
    looking like a worker. A token makes the view load the row.
    """
    User = get_user_model()
    user = User.objects.create_user(
        username=username, email=username, password='pass12345!', is_active=True,
    )
    UserProfile.objects.update_or_create(user=user, defaults={'role': role})
    token = Token.objects.create(user=user)
    client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
    return user


class PermissionTests(TestCase):
    def setUp(self):
        self.family = _family_with_failed_sto()
        self.client = APIClient()

    def test_anonymous_cannot_quote(self):
        res = self.client.get(f'/api/v1/customers/families/{self.family.id}/card/')
        self.assertIn(res.status_code, (401, 403))

    def test_worker_cannot_quote(self):
        _login(self.client, UserProfile.ROLE_WORKER, 'w@x.com')
        res = self.client.get(f'/api/v1/customers/families/{self.family.id}/card/')
        self.assertEqual(res.status_code, 403)

    def test_worker_cannot_replace(self):
        _login(self.client, UserProfile.ROLE_WORKER, 'w2@x.com')
        with patch('apps.core.tranzila_service.TranzilaService.verify_card') as ver:
            res = self.client.post(
                f'/api/v1/customers/families/{self.family.id}/card/replace/',
                {'card_details': CARD}, format='json',
            )
        self.assertEqual(res.status_code, 403)
        ver.assert_not_called()

    def test_manager_sees_the_quote_and_the_link(self):
        _login(self.client, UserProfile.ROLE_MANAGER, 'm@x.com')
        res = self.client.get(f'/api/v1/customers/families/{self.family.id}/card/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['standing_orders'], 1)
        self.assertIn('/replace-card/', res.data['link'])


@patch('apps.core.payment_service.PaymentService._create_invoice_from_payment')
class PublicLinkTests(TestCase):
    def setUp(self):
        self.family = _family_with_failed_sto()
        self.client = APIClient()

    def test_preview_needs_no_login_and_hides_internals(self, _inv):
        token = build_family_token(self.family)
        res = self.client.get(f'/api/v1/customers/replace-card/{token}/')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['items'])
        body = str(res.data)
        self.assertNotIn('OLD', body, 'טוקן הכרטיס לא נחשף להורה')
        self.assertNotIn('recurring_id', body)

    def test_a_broken_token_is_refused_in_hebrew(self, _inv):
        res = self.client.get('/api/v1/customers/replace-card/not-a-token/')
        self.assertEqual(res.status_code, 400)
        self.assertIn('קישור', res.data['error'])

    def test_parent_submits_a_card_and_is_told_only_about_the_money(self, _inv):
        token = build_family_token(self.family)
        with patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=VERIFY_OK), \
             patch('apps.core.tranzila_service.TranzilaService.charge_with_token', return_value=CHARGE_OK):
            res = self.client.post(
                f'/api/v1/customers/replace-card/{token}/apply/',
                {'card_details': CARD}, format='json',
            )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['success'])
        self.assertEqual(res.data['standing_orders_updated'], 1)
        self.assertNotIn('results', res.data, 'ההורה לא מקבל את הפירוט הפנימי')

    def test_a_diners_card_is_refused_with_a_readable_reason(self, _inv):
        token = build_family_token(self.family)
        with patch('apps.core.tranzila_service.TranzilaService.verify_card') as ver:
            res = self.client.post(
                f'/api/v1/customers/replace-card/{token}/apply/',
                {'card_details': {**CARD, 'card_number': '30569309025904'}}, format='json',
            )
        self.assertEqual(res.status_code, 400)
        self.assertIn('דיינרס', res.data['error'])
        ver.assert_not_called()
