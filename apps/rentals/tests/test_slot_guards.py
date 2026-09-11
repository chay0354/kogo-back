"""A slot a rental agreement holds keeps the agreement's rules until the office unlinks it."""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.rentals.models import Tenancy
from apps.rentals.tests.factories import make_branch, make_customer, make_rental, make_user
from apps.scheduling.models import ScheduleEvent

EVENTS = '/api/v1/scheduling/events/'


class HeldSlotTests(APITestCase):
    def setUp(self):
        self.branch = make_branch('פלורנטין')
        self.other = make_branch('רמת אביב')
        self.tenancy = Tenancy.objects.create(
            tenant=make_customer(), branch=self.branch, monthly_amount=Decimal('400'),
        )
        self.held = make_rental(self.branch, renter_id_number='123456789', city=self.branch.city)
        self.held.tenancy = self.tenancy
        self.held.save(update_fields=['tenancy'])
        self.free = make_rental(self.branch, renter_name='אחר', city=self.branch.city)
        self.client.force_authenticate(make_user('manager-guard@test', UserProfile.ROLE_MANAGER))

    def url(self, event):
        return f'{EVENTS}{event.id}/'

    def test_a_held_slot_cannot_move_to_another_branch(self):
        res = self.client.patch(self.url(self.held), {'branch': str(self.other.id)}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.data)
        self.assertIn('branch', res.data)
        self.held.refresh_from_db()
        self.assertEqual(self.held.branch_id, self.branch.id)

    def test_a_held_slot_stays_a_studio_rental(self):
        res = self.client.patch(self.url(self.held), {'is_studio_rental': False}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.data)
        self.assertIn('is_studio_rental', res.data)
        self.held.refresh_from_db()
        self.assertTrue(self.held.is_studio_rental)

    def test_a_held_slot_is_not_deleted_from_under_its_agreement(self):
        res = self.client.delete(self.url(self.held))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('דנה לוי', res.data['error'])
        self.assertTrue(ScheduleEvent.objects.filter(pk=self.held.pk).exists())

    def test_a_slot_no_agreement_holds_is_free_to_go(self):
        self.assertEqual(self.client.delete(self.url(self.free)).status_code, status.HTTP_204_NO_CONTENT)
