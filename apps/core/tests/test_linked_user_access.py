"""
The linked-account switcher is a permission boundary, so it is tested as one.

A link is the only thing that lets one instructor reach another's lessons. The
id travels in the query string, where anyone can put anything, so every test
here asks the same question: does the server check the row, or does it trust
the caller?
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.core.models import LinkedUserAccess, UserProfile

User = get_user_model()


def make_user(username, role=UserProfile.ROLE_WORKER, **extra):
    user = User.objects.create_user(username=username, password='pw-for-tests', **extra)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return user


class LinkedUserAccessTests(APITestCase):
    def setUp(self):
        self.alice = make_user('alice@test', first_name='אליס')
        self.bob = make_user('bob@test', first_name='בוב')
        self.manager = make_user('manager@test', role=UserProfile.ROLE_MANAGER)
        self.url = reverse('auth-linked-users')

    def auth(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_instructor_starts_with_no_links(self):
        self.auth(self.alice)
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['linked_users'], [])

    def test_instructor_cannot_grant_themselves_access(self):
        """The obvious attack: ask for the link you want."""
        self.auth(self.alice)
        res = self.client.post(
            self.url, {'user_id': str(self.alice.id), 'linked_user_id': str(self.bob.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(LinkedUserAccess.objects.exists())

    def test_instructor_cannot_read_someone_elses_links(self):
        self.auth(self.alice)
        res = self.client.get(self.url, {'user_id': str(self.bob.id)})
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_manager_grants_and_revokes(self):
        self.auth(self.manager)
        res = self.client.post(
            self.url, {'user_id': str(self.alice.id), 'linked_user_id': str(self.bob.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(LinkedUserAccess.objects.filter(owner=self.alice, linked=self.bob).exists())

        self.auth(self.alice)
        res = self.client.get(self.url)
        self.assertEqual([u['id'] for u in res.data['linked_users']], [str(self.bob.id)])

        self.auth(self.manager)
        res = self.client.delete(
            f'{self.url}?user_id={self.alice.id}&linked_user_id={self.bob.id}'
        )
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(LinkedUserAccess.objects.exists())

    def test_granting_twice_does_not_duplicate(self):
        self.auth(self.manager)
        payload = {'user_id': str(self.alice.id), 'linked_user_id': str(self.bob.id)}
        self.client.post(self.url, payload)
        self.client.post(self.url, payload)
        self.assertEqual(LinkedUserAccess.objects.count(), 1)

    def test_cannot_link_a_user_to_themselves(self):
        self.auth(self.manager)
        res = self.client.post(
            self.url, {'user_id': str(self.alice.id), 'linked_user_id': str(self.alice.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(LinkedUserAccess.objects.exists())

    def test_link_is_one_way(self):
        """Bob being reachable by Alice must not make Alice reachable by Bob."""
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.bob)
        self.auth(self.bob)
        res = self.client.get(self.url)
        self.assertEqual(res.data['linked_users'], [])


class LinkedUserDashboardAccessTests(APITestCase):
    """The dashboard must refuse an id it was not given a row for."""

    def setUp(self):
        self.alice = make_user('alice2@test')
        self.bob = make_user('bob2@test')
        self.url = reverse('instructor-my-dashboard')

    def auth(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_unlinked_as_user_is_refused(self):
        self.auth(self.alice)
        res = self.client.get(self.url, {'as_user': str(self.bob.id)})
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_linked_as_user_is_allowed(self):
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.bob)
        self.auth(self.alice)
        res = self.client.get(self.url, {'as_user': str(self.bob.id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['subject']['id'], str(self.bob.id))
        self.assertFalse(res.data['subject']['is_self'])

    def test_own_id_needs_no_link(self):
        self.auth(self.alice)
        res = self.client.get(self.url, {'as_user': str(self.alice.id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['subject']['is_self'])

    def test_unknown_user_id_is_refused(self):
        self.auth(self.alice)
        res = self.client.get(self.url, {'as_user': '99999999'})
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_malformed_user_id_is_refused_not_crashed(self):
        """An untrusted string must not reach the ORM as a value error."""
        self.auth(self.alice)
        for junk in ("' OR 1=1--", 'abc', '00000000-0000-0000-0000-000000000000'):
            res = self.client.get(self.url, {'as_user': junk})
            self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN, junk)

    def test_revoking_the_link_closes_access_immediately(self):
        link = LinkedUserAccess.objects.create(owner=self.alice, linked=self.bob)
        self.auth(self.alice)
        self.assertEqual(
            self.client.get(self.url, {'as_user': str(self.bob.id)}).status_code,
            status.HTTP_200_OK,
        )
        link.delete()
        self.assertEqual(
            self.client.get(self.url, {'as_user': str(self.bob.id)}).status_code,
            status.HTTP_403_FORBIDDEN,
        )


class LinkedUserLessonAccessTests(APITestCase):
    """
    The lesson list is the path that also permits marking, so an unlinked id
    must not widen it by a single row.
    """

    def setUp(self):
        from apps.core.models import Branch, City
        from apps.courses.models import Course, CourseType, Lesson
        from apps.instructors.models import Instructor

        self.alice = make_user('alice3@test')
        self.bob = make_user('bob3@test')

        city = City.objects.create(name='עיר בדיקה')
        branch = Branch.objects.create(name='סניף בדיקה', city=city)
        ctype = CourseType.objects.create(name='סוג בדיקה')
        course = Course.objects.create(
            name='חוג בדיקה', branch=branch, course_type=ctype,
            price=Decimal('200.00'), capacity=12
        )

        # Instructor records are matched to logins by email.
        self.bob_instructor = Instructor.objects.create(
            first_name='בוב', last_name='מדריך', email='bob3@test', primary_branch=branch
        )
        self.lesson = Lesson.objects.create(
            course=course, instructor=self.bob_instructor,
            day_of_week=1, start_time='16:00', end_time='17:00', is_recurring=True,
        )

    def auth(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_unlinked_instructor_cannot_list_colleagues_lessons(self):
        self.auth(self.alice)
        res = self.client.get('/api/v1/scheduling/lessons/', {'as_user': str(self.bob.id)})
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_unlinked_instructor_sees_nothing_of_their_own(self):
        """Alice teaches nothing, so her own list is empty — not Bob's."""
        self.auth(self.alice)
        res = self.client.get('/api/v1/scheduling/lessons/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 0)

    def test_linked_instructor_sees_the_colleagues_lessons(self):
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.bob)
        self.auth(self.alice)
        res = self.client.get('/api/v1/scheduling/lessons/', {'as_user': str(self.bob.id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual([str(row['id']) for row in res.data], [str(self.lesson.id)])

    def test_link_does_not_leak_without_asking_for_it(self):
        """Holding a link must not change what Alice sees by default."""
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.bob)
        self.auth(self.alice)
        res = self.client.get('/api/v1/scheduling/lessons/')
        self.assertEqual(len(res.data), 0)
