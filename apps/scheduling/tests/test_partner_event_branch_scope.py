"""A partner books the calendar of their own branches only: no creating or moving an event elsewhere."""
from datetime import date, time

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, Room, UserProfile
from apps.scheduling.models import ScheduleEvent

User = get_user_model()
URL = '/api/v1/scheduling/events/'


def make_user(username, role, branches=()):
    user = User.objects.create_user(username=username, password='pw-for-tests')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    profile.assigned_branches.set(branches)
    return User.objects.get(pk=user.pk)


class PartnerEventBranchScopeTests(APITestCase):
    def setUp(self):
        self.city = City.objects.create(name='תל אביב')
        self.mine = Branch.objects.create(name='פלורנטין', city=self.city)
        self.theirs = Branch.objects.create(name='רמת אביב', city=self.city)
        self.my_room = Room.objects.create(branch=self.mine, name='סטודיו 1')
        self.their_room = Room.objects.create(branch=self.theirs, name='סטודיו 2')
        self.client.force_authenticate(make_user('partner-ev@test', UserProfile.ROLE_PARTNER, [self.mine]))

    def event(self, **overrides):
        body = {
            'name': 'חזרה', 'event_date': '2026-09-20', 'start_time': '10:00', 'end_time': '11:00',
            'event_type': 'one_time', 'city': str(self.city.id), 'branch': str(self.mine.id),
        }
        body.update(overrides)
        return body

    def rental(self, **overrides):
        body = {
            'is_studio_rental': True, 'studio': str(self.my_room.id), 'renter_name': 'סטודיו אור',
            'renter_id_number': '512345678', 'price_per_session': '100',
            'contract_start_date': '2026-09-01', 'contract_end_date': '2027-08-31',
            'start_time': '12:00', 'end_time': '13:00',
        }
        body.update(overrides)
        return self.event(**body)

    def test_events_and_rentals_in_my_branch_are_created(self):
        self.assertEqual(self.client.post(URL, self.event(), format='json').status_code, status.HTTP_201_CREATED)
        res = self.client.post(URL, self.rental(), format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)

    def test_nothing_is_created_in_another_branch_or_in_none(self):
        for body in (
            self.event(branch=str(self.theirs.id)),
            self.rental(branch=str(self.theirs.id), studio=str(self.their_room.id)),
            # No branch: an event the partner could not see again.
            self.event(branch=None),
            # My branch on the event, another branch's studio in it.
            self.rental(studio=str(self.their_room.id)),
        ):
            with self.subTest(body=body):
                res = self.client.post(URL, body, format='json')
                self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN, res.data)
        self.assertFalse(ScheduleEvent.objects.exists())

    def test_an_event_cannot_be_moved_out_of_my_branches(self):
        event = ScheduleEvent.objects.create(
            name='חזרה', event_date=date(2026, 9, 20), start_time=time(10), end_time=time(11),
            branch=self.mine, city=self.city,
        )
        url = f'{URL}{event.id}/'
        for body in ({'branch': str(self.theirs.id)}, {'branch': None}, {'studio': str(self.their_room.id)}):
            with self.subTest(body=body):
                res = self.client.patch(url, body, format='json')
                self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN, res.data)
        event.refresh_from_db()
        self.assertEqual(event.branch_id, self.mine.id)
        self.assertIsNone(event.studio_id)
        # A change that stays in my branch goes through.
        res = self.client.patch(url, {'name': 'חזרה גנרלית'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)

    def test_a_partner_with_no_branch_creates_nothing(self):
        self.client.force_authenticate(make_user('partner-ev-none@test', UserProfile.ROLE_PARTNER))
        self.assertEqual(self.client.post(URL, self.event(), format='json').status_code, status.HTTP_403_FORBIDDEN)

    def test_a_manager_books_any_branch_or_none(self):
        self.client.force_authenticate(make_user('manager-ev@test', UserProfile.ROLE_MANAGER))
        res = self.client.post(URL, self.event(branch=str(self.theirs.id)), format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        res = self.client.post(URL, self.event(branch=None, start_time='14:00', end_time='15:00'), format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
