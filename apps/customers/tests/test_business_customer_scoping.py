"""
A partner finds the business customers of their own branches and the ones with
no branch, changes only their own branches', and a tenant is not deleted from
under its agreement.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.customers.models import BusinessCustomer
from apps.rentals.models import Tenancy

User = get_user_model()
URL = '/api/v1/customers/business-customers/'


def make_user(username, role, branches=()):
    user = User.objects.create_user(username=username, password='pw-for-tests')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    profile.assigned_branches.set(branches)
    return User.objects.get(pk=user.pk)


class BusinessCustomerScopingTests(APITestCase):
    def setUp(self):
        city = City.objects.create(name='עיר')
        self.mine = Branch.objects.create(name='פלורנטין', city=city)
        self.theirs = Branch.objects.create(name='רמת אביב', city=city)
        self.partner = make_user('partner-bc@test', UserProfile.ROLE_PARTNER, [self.mine])
        self.manager = make_user('manager-bc@test', UserProfile.ROLE_MANAGER)
        self.my_customer = BusinessCustomer.objects.create(first_name='רון', last_name='כהן', branch=self.mine)
        self.their_customer = BusinessCustomer.objects.create(first_name='רון', last_name='בר', branch=self.theirs)
        # No branch: filed before merchants carried one. Every partner may find
        # it; only the office changes it.
        self.office_customer = BusinessCustomer.objects.create(first_name='רון', last_name='לוי')

    def ids(self, res):
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        rows = res.data['results'] if isinstance(res.data, dict) else res.data
        return {row['id'] for row in rows}

    def test_a_partner_finds_their_branches_and_the_branchless_never_another_branch(self):
        self.client.force_authenticate(self.partner)
        expected = {str(self.my_customer.id), str(self.office_customer.id)}
        self.assertEqual(self.ids(self.client.get(URL)), expected)
        self.assertEqual(self.ids(self.client.get(URL, {'search': 'רון'})), expected)
        self.assertEqual(self.client.get(f'{URL}{self.office_customer.id}/').status_code, status.HTTP_200_OK)

    def test_a_partner_cannot_open_change_or_delete_another_branchs_merchant(self):
        self.client.force_authenticate(self.partner)
        url = f'{URL}{self.their_customer.id}/'
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.patch(url, {'phone': '050'}, format='json').status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(BusinessCustomer.objects.get(pk=self.their_customer.pk).phone, '')

    def test_a_partner_cannot_change_claim_or_delete_a_branchless_merchant(self):
        # Its card is shared by every branch's documents: the office's to change.
        self.client.force_authenticate(self.partner)
        url = f'{URL}{self.office_customer.id}/'
        for body in ({'phone': '050'}, {'branch_id': str(self.mine.id)}):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch(url, body, format='json').status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_404_NOT_FOUND)
        office = BusinessCustomer.objects.get(pk=self.office_customer.pk)
        self.assertEqual((office.phone, office.branch_id), ('', None))
        self.assertEqual(BusinessCustomer.objects.count(), 3)

    def test_a_partner_with_no_branch_sees_none(self):
        self.client.force_authenticate(make_user('partner-bc-none@test', UserProfile.ROLE_PARTNER))
        self.assertEqual(self.ids(self.client.get(URL)), set())

    def test_a_manager_sees_everyone(self):
        self.client.force_authenticate(self.manager)
        self.assertEqual(len(self.ids(self.client.get(URL))), 3)

    def test_a_tenant_is_not_deleted_from_under_its_agreement(self):
        Tenancy.objects.create(tenant=self.my_customer, branch=self.mine, monthly_amount=Decimal('1'))
        self.client.force_authenticate(self.manager)
        res = self.client.delete(f'{URL}{self.my_customer.id}/')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data['error'], 'לא ניתן למחוק לקוח עסקי שיש לו הסכם שכירות')
        self.assertTrue(BusinessCustomer.objects.filter(pk=self.my_customer.pk).exists())
        self.assertEqual(self.client.delete(f'{URL}{self.office_customer.id}/').status_code, status.HTTP_204_NO_CONTENT)

    def test_a_partners_new_merchant_takes_their_branch_and_stays_in_sight(self):
        # The document dialog opens its merchant form with no branch chosen.
        self.client.force_authenticate(self.partner)
        res = self.client.post(URL, {'first_name': 'נוי', 'last_name': 'שחר'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(BusinessCustomer.objects.get(pk=res.data['id']).branch_id, self.mine.id)
        self.assertIn(str(res.data['id']), self.ids(self.client.get(URL)))

    def test_a_partner_with_two_branches_names_one(self):
        self.client.force_authenticate(make_user('partner-bc-two@test', UserProfile.ROLE_PARTNER, [self.mine, self.theirs]))
        res = self.client.post(URL, {'first_name': 'נוי', 'last_name': 'שחר'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('branch_id', res.data)

    def test_a_partner_cannot_file_a_merchant_in_another_branch(self):
        self.client.force_authenticate(self.partner)
        res = self.client.post(
            URL, {'first_name': 'נוי', 'last_name': 'שחר', 'branch_id': str(self.theirs.id)}, format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        res = self.client.patch(f'{URL}{self.my_customer.id}/', {'branch_id': str(self.theirs.id)}, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(BusinessCustomer.objects.get(pk=self.my_customer.pk).branch_id, self.mine.id)

    def test_a_managers_merchant_may_have_no_branch(self):
        self.client.force_authenticate(self.manager)
        res = self.client.post(URL, {'first_name': 'נוי', 'last_name': 'שחר'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertIsNone(BusinessCustomer.objects.get(pk=res.data['id']).branch_id)
