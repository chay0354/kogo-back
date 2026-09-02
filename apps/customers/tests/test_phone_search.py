"""A phone is found however it was typed: with 972, with +, with dashes, or plain."""
from datetime import date

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.customers.models import Child, Family
from apps.customers.phone_search import phone_query_digits

User = get_user_model()


class PhoneSearchTests(APITestCase):
    def setUp(self):
        city = City.objects.create(name='עיר')
        branch = Branch.objects.create(name='סניף', city=city)
        fam = Family.objects.create(name='משפחת כהן', phone='052-265-9322', branch=branch)
        other = Family.objects.create(name='משפחת לוי', phone='054-1112223', branch=branch)
        self.kid = Child.objects.create(family=fam, first_name='נועה', last_name='כהן',
                                        birth_date=date(2015, 5, 5), gender='female', status='active')
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
        self.assertEqual(phone_query_digits('123'), '')

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
