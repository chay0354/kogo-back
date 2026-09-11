"""A partner reaches the tenancies of their own branches, and nothing else, on every endpoint."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.customers.models import BusinessCustomer
from apps.rentals.models import Tenancy
from apps.rentals.tests.factories import make_branch, make_customer, make_rental, make_user

URL = '/api/v1/rentals/tenancies/'
User = get_user_model()


class PartnerScopingTests(APITestCase):
    def setUp(self):
        self.mine = make_branch('פלורנטין')
        self.theirs = make_branch('רמת אביב')
        self.partner = make_user('partner-rent@test', UserProfile.ROLE_PARTNER, branches=[self.mine])
        self.client.force_authenticate(self.partner)
        self.my_tenancy = Tenancy.objects.create(
            tenant=make_customer('רון', 'כהן', branch=self.mine), branch=self.mine, monthly_amount=Decimal('1'),
        )
        self.their_tenancy = Tenancy.objects.create(
            tenant=make_customer('יעל', 'בר', branch=self.theirs), branch=self.theirs, monthly_amount=Decimal('1'),
        )

    def detail(self, tenancy, action=''):
        return f'{URL}{tenancy.id}/{action}'

    def test_list_and_detail_show_only_my_branches(self):
        self.assertEqual([row['id'] for row in self.client.get(URL).data], [str(self.my_tenancy.id)])
        self.assertEqual(self.client.get(URL, {'branch': str(self.theirs.id)}).data, [])
        self.assertEqual(self.client.get(self.detail(self.their_tenancy)).status_code, status.HTTP_404_NOT_FOUND)

    def test_another_branchs_tenancy_cannot_be_changed_deleted_or_linked(self):
        slot = make_rental(self.theirs)
        for res in (
            self.client.patch(self.detail(self.their_tenancy), {'notes': 'x'}, format='json'),
            self.client.delete(self.detail(self.their_tenancy)),
            self.client.post(self.detail(self.their_tenancy, 'link-slots/'), {'slot_ids': [str(slot.id)]}, format='json'),
            self.client.post(self.detail(self.their_tenancy, 'unlink-slot/'), {'slot_id': str(slot.id)}, format='json'),
        ):
            self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Tenancy.objects.filter(pk=self.their_tenancy.pk).exists())
        slot.refresh_from_db()
        self.assertIsNone(slot.tenancy_id)

    def test_creates_only_in_my_branches(self):
        body = {'tenant': {'first_name': 'דני'}, 'monthly_amount': '100'}
        elsewhere = self.client.post(URL, {**body, 'branch': str(self.theirs.id)}, format='json')
        self.assertEqual(elsewhere.status_code, status.HTTP_403_FORBIDDEN)
        nowhere = self.client.post(URL, body, format='json')
        self.assertEqual(nowhere.status_code, status.HTTP_403_FORBIDDEN)
        mine = self.client.post(URL, {**body, 'branch': str(self.mine.id)}, format='json')
        self.assertEqual(mine.status_code, status.HTTP_201_CREATED, mine.data)
        # The refused attempts left no merchant behind.
        self.assertEqual(BusinessCustomer.objects.filter(first_name='דני').count(), 1)
        self.assertEqual(Tenancy.objects.filter(branch=self.theirs).count(), 1)

    def test_cannot_move_a_tenancy_out_of_my_branches(self):
        for branch in (str(self.theirs.id), None):
            with self.subTest(branch=branch):
                res = self.client.patch(self.detail(self.my_tenancy), {'branch': branch}, format='json')
                self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.my_tenancy.refresh_from_db()
        self.assertEqual(self.my_tenancy.branch_id, self.mine.id)

    def test_attaches_only_a_merchant_it_can_see(self):
        stranger = make_customer('זר', 'אחר', branch=self.theirs)
        res = self.client.post(URL, {
            'branch': str(self.mine.id), 'tenant_id': str(stranger.id), 'monthly_amount': '1',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('tenant_id', res.data)

    def test_cannot_edit_a_merchant_card_outside_my_branches(self):
        # A manager attached a merchant from another branch to this tenancy.
        self.my_tenancy.tenant = make_customer('זר', 'אחר', branch=self.theirs)
        self.my_tenancy.save()
        res = self.client.patch(self.detail(self.my_tenancy), {'tenant': {'phone': '050'}}, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(BusinessCustomer.objects.get(first_name='זר').phone, '')

    def test_links_only_rentals_of_my_branches(self):
        elsewhere = make_rental(self.theirs)
        res = self.client.post(self.detail(self.my_tenancy, 'link-slots/'), {'slot_ids': [str(elsewhere.id)]}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        # Reported as not found: an id from another branch reveals nothing.
        self.assertEqual(res.data['error'], 'השכירות לא נמצאה')

    def test_suggestions_and_import_stay_in_my_branches(self):
        mine = make_rental(self.mine, renter_id_number='512345678')
        theirs = make_rental(self.theirs, renter_id_number='512345678')
        make_customer('סטודיו', 'אור', company_number='512345678', branch=self.theirs)

        groups = self.client.get(f'{URL}suggestions/').data
        self.assertEqual([group['key'] for group in groups], [f'id:512345678:{self.mine.id}'])
        # The merchant on file is another branch's: not offered to this partner.
        self.assertIsNone(groups[0]['existing_tenant'])

        group = {'tenant': {'first_name': 'א'}, 'monthly_amount': '1'}
        res = self.client.post(f'{URL}import/', {'groups': [{**group, 'slot_ids': [str(theirs.id)]}]}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data['error'], 'קבוצה 1: השכירות לא נמצאה')
        res = self.client.post(f'{URL}import/', {'groups': [{**group, 'slot_ids': [str(mine.id)]}]}, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(str(res.data[0]['branch']), str(self.mine.id))

    def test_a_partner_with_no_branch_sees_and_creates_nothing(self):
        self.client.force_authenticate(make_user('partner-none@test', UserProfile.ROLE_PARTNER))
        make_rental(self.mine, renter_id_number='1')
        self.assertEqual(self.client.get(URL).data, [])
        self.assertEqual(self.client.get(f'{URL}suggestions/').data, [])
        res = self.client.post(URL, {
            'branch': str(self.mine.id), 'tenant': {'first_name': 'א'}, 'monthly_amount': '1',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_manager_reaches_every_branch(self):
        self.client.force_authenticate(make_user('manager-all@test', UserProfile.ROLE_MANAGER))
        self.assertEqual(len(self.client.get(URL).data), 2)

    def test_instructors_and_accounts_without_a_profile_are_refused(self):
        worker = make_user('worker-rent@test', UserProfile.ROLE_WORKER)
        orphan = User.objects.create_user(username='orphan-rent@test', password='pw-for-tests')
        UserProfile.objects.filter(user=orphan).delete()
        orphan = User.objects.get(pk=orphan.pk)
        for user in (worker, orphan):
            self.client.force_authenticate(user)
            for res in (
                self.client.get(URL),
                self.client.get(self.detail(self.my_tenancy)),
                self.client.get(f'{URL}suggestions/'),
                self.client.post(f'{URL}import/', {'groups': []}, format='json'),
                self.client.post(URL, {}, format='json'),
            ):
                self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
