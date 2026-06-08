"""Workers (instructors) must not see studio rentals."""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, UserProfile
from apps.instructors.models import Instructor
from apps.scheduling.models import ScheduleEvent


User = get_user_model()


def _worker_client(email):
    user = User.objects.create_user(username=email, email=email, password='x')
    UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_WORKER})
    client = APIClient()
    token, _ = Token.objects.get_or_create(user=user)
    client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
    return client, user


class WorkerRentalVisibilityTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='Main')
        self.instructor = Instructor.objects.create(
            first_name='Chay',
            last_name='Test',
            email='inst@test.com',
            phone='0501111111',
        )
        self.worker_client, _ = _worker_client('inst@test.com')

        self.rental = ScheduleEvent.objects.create(
            name='Studio rental',
            event_date=date.today(),
            start_time='10:00',
            end_time='11:00',
            branch=self.branch,
            is_studio_rental=True,
            renter_name='External',
        )
        self.assigned_event = ScheduleEvent.objects.create(
            name='Team meeting',
            event_date=date.today(),
            start_time='12:00',
            end_time='13:00',
            branch=self.branch,
            is_studio_rental=False,
        )
        self.assigned_event.assigned_instructors.add(self.instructor)

    def test_worker_does_not_see_studio_rentals_on_schedule(self):
        res = self.worker_client.get('/api/v1/scheduling/events/')
        self.assertEqual(res.status_code, 200)
        names = {item['name'] for item in res.data}
        self.assertIn('Team meeting', names)
        self.assertNotIn('Studio rental', names)

    def test_worker_studio_rental_list_is_empty(self):
        res = self.worker_client.get('/api/v1/scheduling/events/?studio_rental=1')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data), 0)
