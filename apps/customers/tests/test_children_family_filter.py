"""The child card opens a sibling through the list endpoint's family filter."""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, UserProfile
from apps.customers.models import Child, Family


class ChildrenFamilyFilterTest(TestCase):
    def setUp(self):
        branch = Branch.objects.create(name='Main')
        self.family = Family.objects.create(name='Levi', phone='0521111111', branch=branch)
        self.big = Child.objects.create(
            family=self.family, first_name='Noa', last_name='Levi',
            birth_date=date(2015, 3, 1), gender='female', status='active',
        )
        self.small = Child.objects.create(
            family=self.family, first_name='Ido', last_name='Levi',
            birth_date=date(2018, 3, 1), gender='male', status='trial_signed',
        )
        other = Family.objects.create(name='Mizrahi', phone='0522222222', branch=branch)
        Child.objects.create(
            family=other, first_name='Dana', last_name='Mizrahi',
            birth_date=date(2016, 1, 1), gender='female', status='active',
        )
        User = get_user_model()
        manager = User.objects.create_user(username='m@test.com', email='m@test.com', password='x')
        UserProfile.objects.update_or_create(user=manager, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=manager).key}')

    def test_a_family_filter_returns_that_family_alone_as_full_cards(self):
        res = self.client.get('/api/v1/customers/children/', {'family': str(self.family.id)})
        self.assertEqual(res.status_code, 200)
        names = sorted(row['first_name'] for row in res.data['results'])
        self.assertEqual(names, ['Ido', 'Noa'])
        # The list serializer is the one that carries siblings — the reason the card uses it.
        noa = next(row for row in res.data['results'] if row['first_name'] == 'Noa')
        self.assertEqual([s['first_name'] for s in noa['siblings']], ['Ido'])

    def test_an_unknown_family_returns_nothing(self):
        res = self.client.get('/api/v1/customers/children/', {'family': '00000000-0000-0000-0000-000000000000'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['count'], 0)

    def test_a_family_id_that_is_not_a_uuid_answers_empty(self):
        res = self.client.get('/api/v1/customers/children/', {'family': 'not-a-uuid'})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['count'], 0)
