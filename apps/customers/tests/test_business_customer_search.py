"""Finding a merchant by any detail printed on their card."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.customers.models import BusinessCustomer

User = get_user_model()
URL = '/api/v1/customers/business-customers/'


class BusinessCustomerSearchTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        user = User.objects.create_user(
            username='mgr-search@x.com', email='mgr-search@x.com',
            password='pass12345!', is_active=True,
        )
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        token = Token.objects.create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

        self.customer = BusinessCustomer.objects.create(
            first_name='אולמי', last_name='הגן',
            email='events@gan.co.il', phone='0521234567',
            id_number='123456782', company_number='514999888',
            address='הרצל 10, תל אביב',
        )
        BusinessCustomer.objects.create(first_name='אחר', last_name='לגמרי', email='no@way.com')

    def _search(self, term):
        res = self.client.get(URL, {'search': term})
        self.assertEqual(res.status_code, 200)
        rows = res.data.get('results', res.data)
        return [r['id'] for r in rows]

    def test_by_email(self):
        self.assertEqual(self._search('events@gan.co.il'), [str(self.customer.id)])

    def test_by_part_of_an_email(self):
        self.assertEqual(self._search('events'), [str(self.customer.id)])

    def test_by_id_number(self):
        self.assertEqual(self._search('123456782'), [str(self.customer.id)])

    def test_by_company_number(self):
        self.assertEqual(self._search('514999888'), [str(self.customer.id)])

    def test_by_phone(self):
        self.assertEqual(self._search('0521234567'), [str(self.customer.id)])

    def test_by_address(self):
        self.assertEqual(self._search('הרצל'), [str(self.customer.id)])

    def test_by_the_whole_name_typed_as_one_string(self):
        self.assertEqual(self._search('אולמי הגן'), [str(self.customer.id)])

    def test_by_the_family_name_alone(self):
        self.assertEqual(self._search('הגן'), [str(self.customer.id)])

    def test_something_nobody_has_finds_nobody(self):
        self.assertEqual(self._search('זזזזז'), [])


class BusinessCustomerUpdateTests(TestCase):
    """Editing a saved merchant has to persist — the dialog used to drop it."""

    def setUp(self):
        self.client = APIClient()
        user = User.objects.create_user(
            username='mgr-upd@x.com', email='mgr-upd@x.com', password='pass12345!', is_active=True,
        )
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        token = Token.objects.create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        self.customer = BusinessCustomer.objects.create(
            first_name='שם', last_name='ישן', email='old@x.com',
        )

    def test_patch_saves_the_new_details(self):
        res = self.client.patch(
            f'{URL}{self.customer.id}/',
            {'first_name': 'שם', 'last_name': 'חדש', 'email': 'new@x.com'},
            format='json',
        )
        self.assertEqual(res.status_code, 200)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.last_name, 'חדש')
        self.assertEqual(self.customer.email, 'new@x.com')

    def test_the_edited_customer_is_found_by_its_new_email(self):
        self.client.patch(f'{URL}{self.customer.id}/', {'email': 'new@x.com'}, format='json')
        res = self.client.get(URL, {'search': 'new@x.com'})
        rows = res.data.get('results', res.data)
        self.assertEqual([r['id'] for r in rows], [str(self.customer.id)])
