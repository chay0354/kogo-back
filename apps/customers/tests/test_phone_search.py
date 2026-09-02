"""A phone is found however it was typed: with 972, with +, with dashes, or plain."""
from datetime import date

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.customers.models import Child, Family, Parent
from apps.customers.phone_search import phone_query_digits

User = get_user_model()


class PhoneSearchTests(APITestCase):
    def setUp(self):
        city = City.objects.create(name='עיר')
        branch = Branch.objects.create(name='סניף', city=city)
        fam = Family.objects.create(name='משפחת כהן', phone='052-265-9322', branch=branch)
        other = Family.objects.create(name='משפחת לוי', phone='054-1112223', branch=branch)
        self.kid = Child.objects.create(family=fam, first_name='נועה', last_name='כהן', id_number='031972543',
                                        birth_date=date(2015, 5, 5), gender='female', status='active')
        fam.parent_id_number = '012345678'
        fam.save(update_fields=['parent_id_number'])
        Parent.objects.create(family=fam, first_name='יעל', last_name='כהן', phone='050-777-8899', email='yael@example.com', is_primary=True)
        Child.objects.create(family=other, first_name='דן', last_name='לוי',
                             birth_date=date(2014, 3, 3), gender='male', status='active')
        user = User.objects.create_user(username='m@test', password='pw')
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save(update_fields=['role'])
        self.client.force_authenticate(User.objects.get(pk=user.pk))

    def test_query_normalisation(self):
        self.assertEqual(phone_query_digits('972522659322'), '0522659322')
        self.assertEqual(phone_query_digits('+972 52-265-9322'), '0522659322')
        self.assertEqual(phone_query_digits('052-2659322'), '0522659322')
        self.assertEqual(phone_query_digits('נועה'), '')
        self.assertEqual(phone_query_digits('12'), '')
        # A partial number with the country code is the prefix the reader means.
        self.assertEqual(phone_query_digits('97254'), '054')
        self.assertEqual(phone_query_digits('9725'), '05')

    def _ids(self, url, search):
        res = self.client.get(url, {'search': search})
        self.assertEqual(res.status_code, 200, res.content[:200])
        body = res.data
        rows = body['results'] if isinstance(body, dict) and 'results' in body else body
        return {str(r['id']) for r in rows}

    def test_children_found_by_972_phone(self):
        for typed in ('972522659322', '+972522659322', '+972 52-265-9322', '0522659322', '052-265-9322'):
            ids = self._ids('/api/v1/customers/children/', typed)
            self.assertEqual(ids, {str(self.kid.id)}, typed)

    def test_families_found_by_972_phone(self):
        ids = self._ids('/api/v1/customers/families/', '972522659322')
        self.assertEqual(len(ids), 1)

    def test_name_search_still_works(self):
        ids = self._ids('/api/v1/customers/children/', 'נועה')
        self.assertEqual(ids, {str(self.kid.id)})

    def test_child_found_by_parent_name_phone_and_id(self):
        self.assertEqual(self._ids('/api/v1/customers/children/', 'יעל'), {str(self.kid.id)})
        self.assertEqual(self._ids('/api/v1/customers/children/', '9725077788'), {str(self.kid.id)})
        self.assertEqual(self._ids('/api/v1/customers/children/', '031972543'), {str(self.kid.id)})

    def test_partial_972_prefix_finds_phones_by_the_exact_digit_run(self):
        # '97254' means the 054- prefix; only phones actually starting 054 match,
        # and an ID number is matched only if it holds the digits as typed.
        Family.objects.create(name='משפחת 054', phone='054-646-9155', branch=self.kid.family.branch)
        kid054 = Child.objects.create(family=Family.objects.get(name='משפחת 054'), first_name='רום', last_name='ב',
                                      birth_date=date(2014, 1, 1), gender='male', status='active')
        # Every 054 phone answers to the prefix — the other family is 054 too —
        # and so does an ID number that holds the digits as typed (031972543).
        found = self._ids('/api/v1/customers/children/', '97254')
        self.assertIn(str(kid054.id), found)
        self.assertNotIn(str(kid054.id), self._ids('/api/v1/customers/children/', '97255'))
        self.assertEqual(self._ids('/api/v1/customers/children/', '972546469'), {str(kid054.id)})
        # '46469' is inside that phone; '46479' is not — nothing approximate.
        self.assertEqual(self._ids('/api/v1/customers/children/', '46469'), {str(kid054.id)})
        self.assertEqual(self._ids('/api/v1/customers/children/', '46479'), set())
        # An ID number is a raw digit run: '97254' is in 031972543, '97256' is not.
        self.assertIn(str(self.kid.id), self._ids('/api/v1/customers/children/', '97254'))
        self.assertNotIn(str(self.kid.id), self._ids('/api/v1/customers/children/', '97256'))

    def _rows(self, search):
        res = self.client.get('/api/v1/customers/children/', {'search': search})
        body = res.data
        return body['results'] if isinstance(body, dict) and 'results' in body else body

    def test_row_says_what_matched_when_it_is_not_on_the_row(self):
        row = next(r for r in self._rows('יעל') if str(r['id']) == str(self.kid.id))
        self.assertEqual(row['search_match'], {'label': 'שם הורה', 'value': 'יעל כהן'})
        row = next(r for r in self._rows('031972543') if str(r['id']) == str(self.kid.id))
        self.assertEqual(row['search_match'], {'label': 'ת.ז. ילד', 'value': '031972543'})
        # The parent's phone is what the row shows, so a hit on it needs no label.
        row = next(r for r in self._rows('9725077788') if str(r['id']) == str(self.kid.id))
        self.assertIsNone(row['search_match'])
        row = next(r for r in self._rows('נועה') if str(r['id']) == str(self.kid.id))
        self.assertIsNone(row['search_match'])

    def test_no_search_no_match_field_value(self):
        res = self.client.get('/api/v1/customers/children/')
        body = res.data
        rows = body['results'] if isinstance(body, dict) and 'results' in body else body
        self.assertTrue(all(r.get('search_match') is None for r in rows))

    def test_profile_carries_parent_id_number_and_email(self):
        row = next(r for r in self._rows('נועה') if str(r['id']) == str(self.kid.id))
        self.assertEqual(row['parent_id_number'], '012345678')
        self.assertEqual(row['parent_email'], 'yael@example.com')
        # parent_id stays the parent row's key, never shown as an ID number
        self.assertNotEqual(row['parent_id'], row['parent_id_number'])

    def test_child_found_by_parent_email(self):
        self.assertEqual(self._ids('/api/v1/customers/children/', 'yael@'), {str(self.kid.id)})
