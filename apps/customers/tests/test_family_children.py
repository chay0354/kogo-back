"""The family card carries who is already in it — what the add-a-child screen shows."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory

User = get_user_model()
URL = '/api/v1/customers/families/'


class FamilyChildrenTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        user = User.objects.create_user(username='mgr-fam@x.com', email='mgr-fam@x.com',
                                        password='pass12345!', is_active=True)
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')

        self.family = TestDataFactory.create_family(name='משפחת בדיקה')
        TestDataFactory.create_parent(family=self.family)
        self.a = TestDataFactory.create_child(family=self.family, first_name='אלף', status='active')
        self.b = TestDataFactory.create_child(family=self.family, first_name='בית', status='trial_signed')

    def _row(self):
        res = self.client.get(URL, {'search': 'משפחת בדיקה'})
        self.assertEqual(res.status_code, 200)
        rows = res.data.get('results', res.data)
        return next(r for r in rows if r['id'] == str(self.family.id))

    def test_the_children_come_with_the_family(self):
        names = {c['first_name'] for c in self._row()['children']}
        self.assertEqual(names, {'אלף', 'בית'})

    def test_each_child_carries_what_identifies_them(self):
        child = next(c for c in self._row()['children'] if c['first_name'] == 'אלף')
        self.assertTrue(child['full_name'])
        self.assertEqual(child['status'], 'active')
        self.assertTrue(child['status_display'])
        self.assertIn('birth_date', child)

    def test_a_status_is_readable_not_just_a_code(self):
        child = next(c for c in self._row()['children'] if c['first_name'] == 'בית')
        self.assertEqual(child['status_display'], 'נרשם לניסיון')

    def test_the_parents_are_still_there(self):
        self.assertTrue(self._row()['parents'])

    def test_a_family_with_no_children_answers_an_empty_list(self):
        empty = TestDataFactory.create_family(name='משפחה ריקה')
        res = self.client.get(URL, {'search': 'משפחה ריקה'})
        row = next(r for r in res.data.get('results', res.data) if r['id'] == str(empty.id))
        self.assertEqual(row['children'], [])

    def test_children_are_read_only(self):
        res = self.client.patch(f'{URL}{self.family.id}/', {'children': []}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self._row()['children']), 2)
