"""A tenant's name: a business has one, a person two — the tenants screen writes a business's as last_name."""
from rest_framework import status

from apps.rentals.tests.test_tenancy_api import URL, TenancyApiTestCase


class TenantNameTests(TenancyApiTestCase):
    def test_a_business_is_its_one_name(self):
        res = self.create(tenant={'last_name': 'סטודיו תנועה בע"מ', 'company_number': '512345678'})
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data['tenant']['full_name'], 'סטודיו תנועה בע"מ')

    def test_a_first_name_alone_will_do(self):
        res = self.create(tenant={'first_name': 'דנה'})
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data['tenant']['full_name'], 'דנה')

    def test_a_tenant_without_any_name_is_refused(self):
        res = self.create(tenant={'first_name': ' ', 'last_name': '', 'phone': '050-1234567'})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('last_name', res.data['tenant'])

    def test_an_edit_may_clear_one_name_and_keep_the_other(self):
        created = self.create(tenant={'first_name': 'דנה', 'last_name': 'לוי'}).data
        res = self.client.patch(f'{URL}{created["id"]}/', {'tenant': {'first_name': ''}}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(res.data['tenant']['full_name'], 'לוי')

    def test_an_edit_cannot_clear_both_names(self):
        created = self.create(tenant={'first_name': 'דנה', 'last_name': 'לוי'}).data
        res = self.client.patch(
            f'{URL}{created["id"]}/', {'tenant': {'first_name': '', 'last_name': ''}}, format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
