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


class LinkedUserBranchApiTests(APITestCase):
    """Granting one branch of a colleague, and reading back which one it is."""

    def setUp(self):
        from apps.core.models import Branch, City

        self.alice = make_user('alice4@test', first_name='אליס')
        self.bob = make_user('bob4@test', first_name='בוב')
        self.manager = make_user('manager4@test', role=UserProfile.ROLE_MANAGER)
        self.url = reverse('auth-linked-users')

        city = City.objects.create(name='עיר בדיקה')
        self.north = Branch.objects.create(name='סניף צפון', city=city)
        self.south = Branch.objects.create(name='סניף דרום', city=city)

    def auth(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_link_without_a_branch_covers_everything(self):
        self.auth(self.manager)
        res = self.client.post(
            self.url, {'user_id': str(self.alice.id), 'linked_user_id': str(self.bob.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(res.data['branch_id'])
        self.assertIsNone(res.data['branch_name'])

    def test_link_can_be_limited_to_one_branch(self):
        self.auth(self.manager)
        res = self.client.post(self.url, {
            'user_id': str(self.alice.id),
            'linked_user_id': str(self.bob.id),
            'branch_id': str(self.north.id),
        })
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['branch_id'], str(self.north.id))
        self.assertEqual(res.data['branch_name'], 'סניף צפון')

        self.auth(self.alice)
        res = self.client.get(self.url)
        self.assertEqual(res.data['linked_users'][0]['branch_id'], str(self.north.id))
        self.assertEqual(res.data['linked_users'][0]['branch_name'], 'סניף צפון')

    def test_granting_again_moves_the_limit_instead_of_adding_a_row(self):
        """One row per colleague, so the screen can never show two rules at once."""
        self.auth(self.manager)
        for branch_id in (str(self.north.id), str(self.south.id), ''):
            payload = {'user_id': str(self.alice.id), 'linked_user_id': str(self.bob.id)}
            if branch_id:
                payload['branch_id'] = branch_id
            self.client.post(self.url, payload)

        self.assertEqual(LinkedUserAccess.objects.count(), 1)
        self.assertIsNone(LinkedUserAccess.objects.get().branch_id)

    def test_unknown_branch_is_refused(self):
        self.auth(self.manager)
        for junk in ('99999999', 'abc', '00000000-0000-0000-0000-000000000000'):
            res = self.client.post(self.url, {
                'user_id': str(self.alice.id),
                'linked_user_id': str(self.bob.id),
                'branch_id': junk,
            })
            self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND, junk)
            self.assertFalse(LinkedUserAccess.objects.exists(), junk)

    def test_instructor_cannot_grant_themselves_a_branch(self):
        """A branch on the payload must not turn a refused grant into a grant."""
        self.auth(self.alice)
        res = self.client.post(self.url, {
            'user_id': str(self.alice.id),
            'linked_user_id': str(self.bob.id),
            'branch_id': str(self.north.id),
        })
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(LinkedUserAccess.objects.exists())


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


class LinkedUserBranchScopeTests(APITestCase):
    """
    A link limited to one branch is a smaller permission, not a hint.

    Bob teaches in two branches. Every test here hands Alice a link to the north
    branch and then tries to reach the south one — through the list, through a
    single lesson, through the register, and through the dashboard.
    """

    def setUp(self):
        from apps.core.models import Branch, City
        from apps.courses.models import Course, CourseType, Lesson
        from apps.instructors.models import Instructor

        self.alice = make_user('alice5@test')
        self.bob = make_user('bob5@test')
        self.manager = make_user('manager5@test', role=UserProfile.ROLE_MANAGER)

        city = City.objects.create(name='עיר בדיקה')
        self.north = Branch.objects.create(name='סניף צפון', city=city)
        self.south = Branch.objects.create(name='סניף דרום', city=city)
        ctype = CourseType.objects.create(name='סוג בדיקה')

        self.bob_instructor = Instructor.objects.create(
            first_name='בוב', last_name='מדריך', email='bob5@test', primary_branch=self.north
        )
        self.north_lesson = self._lesson('חוג צפון', self.north, ctype, day_of_week=1)
        self.south_lesson = self._lesson('חוג דרום', self.south, ctype, day_of_week=2)

    def _lesson(self, name, branch, ctype, day_of_week):
        from apps.courses.models import Course, Lesson

        course = Course.objects.create(
            name=name, branch=branch, course_type=ctype,
            price=Decimal('200.00'), capacity=12,
        )
        return Lesson.objects.create(
            course=course, instructor=self.bob_instructor,
            day_of_week=day_of_week, start_time='16:00', end_time='17:00', is_recurring=True,
        )

    def auth(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def lesson_ids(self):
        res = self.client.get('/api/v1/scheduling/lessons/', {'as_user': str(self.bob.id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        return {str(row['id']) for row in res.data}

    def test_branch_scoped_link_lists_only_that_branch(self):
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.bob, branch=self.north)
        self.auth(self.alice)
        self.assertEqual(self.lesson_ids(), {str(self.north_lesson.id)})

    def test_unscoped_link_still_lists_everything(self):
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.bob)
        self.auth(self.alice)
        self.assertEqual(
            self.lesson_ids(),
            {str(self.north_lesson.id), str(self.south_lesson.id)},
        )

    def open_lesson(self, lesson):
        return self.client.get(
            f'/api/v1/scheduling/lessons/{lesson.id}/',
            {'as_user': str(self.bob.id), 'date': '2026-09-01'},
        )

    def mark_register(self, lesson):
        return self.client.post(
            f'/api/v1/scheduling/lessons/{lesson.id}/mark_attendance/'
            f'?as_user={self.bob.id}',
            {'date': '2026-09-01', 'attendance': []},
            format='json',
        )

    def test_branch_scoped_link_cannot_open_a_lesson_elsewhere(self):
        """The list hiding it is not enough — the id is guessable."""
        link = LinkedUserAccess.objects.create(
            owner=self.alice, linked=self.bob, branch=self.north
        )
        self.auth(self.alice)
        self.assertEqual(self.open_lesson(self.north_lesson).status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.open_lesson(self.south_lesson).status_code, status.HTTP_404_NOT_FOUND
        )

        # The same id opens once the limit is lifted, so the 404 above is the
        # branch talking and not some unrelated refusal.
        link.branch = None
        link.save(update_fields=['branch'])
        self.assertEqual(self.open_lesson(self.south_lesson).status_code, status.HTTP_200_OK)

    def test_branch_scoped_link_cannot_mark_a_register_elsewhere(self):
        """Covering a colleague includes marking, so the limit has to reach it."""
        link = LinkedUserAccess.objects.create(
            owner=self.alice, linked=self.bob, branch=self.north
        )
        self.auth(self.alice)
        self.assertEqual(self.mark_register(self.north_lesson).status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.mark_register(self.south_lesson).status_code, status.HTTP_404_NOT_FOUND
        )

        link.branch = None
        link.save(update_fields=['branch'])
        self.assertEqual(self.mark_register(self.south_lesson).status_code, status.HTTP_200_OK)

    def test_moving_the_branch_takes_effect_immediately(self):
        link = LinkedUserAccess.objects.create(
            owner=self.alice, linked=self.bob, branch=self.north
        )
        self.auth(self.alice)
        self.assertEqual(self.lesson_ids(), {str(self.north_lesson.id)})

        link.branch = self.south
        link.save(update_fields=['branch'])
        self.assertEqual(self.lesson_ids(), {str(self.south_lesson.id)})

        link.branch = None
        link.save(update_fields=['branch'])
        self.assertEqual(
            self.lesson_ids(),
            {str(self.north_lesson.id), str(self.south_lesson.id)},
        )

    def test_manager_switching_is_not_narrowed_by_someone_elses_link(self):
        """A row belongs to its owner; it must not shrink what a manager sees."""
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.bob, branch=self.north)
        self.auth(self.manager)
        res = self.client.get('/api/v1/scheduling/lessons/', {'as_user': str(self.bob.id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 2)

    def dashboard_branches(self):
        res = self.client.get(
            reverse('instructor-my-dashboard'), {'as_user': str(self.bob.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        return {b['name'] for b in res.data['branches']}, {
            g['course_name'] for g in res.data['groups']
        }

    def test_dashboard_obeys_the_branch_limit(self):
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.bob, branch=self.north)
        self.auth(self.alice)
        branches, groups = self.dashboard_branches()
        self.assertEqual(branches, {'סניף צפון'})
        self.assertEqual(groups, {'חוג צפון'})

    def test_dashboard_unscoped_link_still_counts_everything(self):
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.bob)
        self.auth(self.alice)
        branches, groups = self.dashboard_branches()
        self.assertEqual(branches, {'סניף צפון', 'סניף דרום'})
        self.assertEqual(groups, {'חוג צפון', 'חוג דרום'})

    def test_dashboard_branch_param_cannot_reach_past_the_limit(self):
        """The client picks a branch too; its pick must not widen the link."""
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.bob, branch=self.north)
        self.auth(self.alice)
        res = self.client.get(reverse('instructor-my-dashboard'), {
            'as_user': str(self.bob.id),
            'branch_id': str(self.south.id),
        })
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['groups'], [])

    def test_own_view_is_never_narrowed_by_a_link_they_hold(self):
        """Alice's limit on Bob says nothing about Alice's own timetable."""
        from apps.instructors.models import Instructor

        Instructor.objects.create(
            first_name='אליס', last_name='מדריכה', email='alice5@test',
            primary_branch=self.south,
        )
        alice_lesson = self._lesson_for_alice()
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.bob, branch=self.north)
        self.auth(self.alice)
        res = self.client.get('/api/v1/scheduling/lessons/')
        self.assertEqual([str(row['id']) for row in res.data], [str(alice_lesson.id)])

    def _lesson_for_alice(self):
        from apps.courses.models import Course, CourseType, Lesson
        from apps.instructors.models import Instructor

        instructor = Instructor.objects.get(email='alice5@test')
        course = Course.objects.create(
            name='חוג של אליס', branch=self.south,
            course_type=CourseType.objects.first(),
            price=Decimal('200.00'), capacity=12,
        )
        return Lesson.objects.create(
            course=course, instructor=instructor,
            day_of_week=3, start_time='16:00', end_time='17:00', is_recurring=True,
        )


class LinkedSubjectWithoutAnInstructorRecordTests(APITestCase):
    """
    A link hands over a colleague's reach and never more than it.

    The dashboard narrows what it counts by the subject's Instructor row. When
    the linked account has no such row there is nothing to narrow by, and what
    is left over decides the answer: an unnarrowed screen belongs to a manager
    reading their own numbers, and a link must not be a way to buy one.
    """

    def setUp(self):
        from apps.core.models import Branch, City
        from apps.courses.models import CourseType
        from apps.instructors.models import Instructor

        self.alice = make_user('alice6@test', first_name='אליס')
        self.bob = make_user('bob6@test', first_name='בוב')
        # A manager account with no Instructor row — the shape the link abuses.
        self.chief = make_user('chief6@test', role=UserProfile.ROLE_MANAGER)
        self.partner = make_user('partner6@test', role=UserProfile.ROLE_PARTNER)

        city = City.objects.create(name='עיר בדיקה')
        self.north = Branch.objects.create(name='סניף צפון', city=city)
        self.south = Branch.objects.create(name='סניף דרום', city=city)
        self.ctype = CourseType.objects.create(name='סוג בדיקה')

        bob_instructor = Instructor.objects.create(
            first_name='בוב', last_name='מדריך', email='bob6@test', primary_branch=self.north
        )
        # Nobody holds a link to Dana. Her group is what "everything" looks like.
        dana_instructor = Instructor.objects.create(
            first_name='דנה', last_name='מדריכה', email='dana6@test', primary_branch=self.south
        )
        self.bob_lesson = self._lesson('חוג של בוב', self.north, bob_instructor, 1)
        self.dana_lesson = self._lesson('חוג של דנה', self.south, dana_instructor, 2)
        self.url = reverse('instructor-my-dashboard')

    def _lesson(self, name, branch, instructor, day_of_week):
        from apps.courses.models import Course, Lesson

        course = Course.objects.create(
            name=name, branch=branch, course_type=self.ctype,
            price=Decimal('200.00'), capacity=12,
        )
        return Lesson.objects.create(
            course=course, instructor=instructor,
            day_of_week=day_of_week, start_time='16:00', end_time='17:00', is_recurring=True,
        )

    def auth(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def groups(self, **params):
        res = self.client.get(self.url, params)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        return {g['course_name'] for g in res.data['groups']}

    def test_link_to_an_account_without_an_instructor_record_counts_nothing(self):
        """The manager teaches nothing, so the link inherits nothing."""
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.chief)
        self.auth(self.alice)
        self.assertEqual(self.groups(as_user=str(self.chief.id)), set())

    def test_the_same_caller_still_sees_a_linked_instructors_groups(self):
        """Positive control: the empty answer above is the rule, not the fixture."""
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.bob)
        self.auth(self.alice)
        self.assertEqual(self.groups(as_user=str(self.bob.id)), {'חוג של בוב'})

    def test_the_lesson_list_through_that_link_is_already_empty(self):
        """The register path narrows by identity whatever the subject is."""
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.chief)
        self.auth(self.alice)
        res = self.client.get('/api/v1/scheduling/lessons/', {'as_user': str(self.chief.id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 0)

    def test_the_managers_own_dashboard_still_counts_everything(self):
        self.auth(self.chief)
        self.assertEqual(self.groups(), {'חוג של בוב', 'חוג של דנה'})

    def test_a_manager_switching_accounts_is_unchanged(self):
        self.auth(self.chief)
        self.assertEqual(self.groups(as_user=str(self.bob.id)), {'חוג של בוב'})

    def test_a_partners_own_dashboard_is_unchanged(self):
        """A partner has no register screen here, and gains none from this."""
        self.partner.profile.assigned_branches.add(self.north)
        self.auth(self.partner)
        self.assertEqual(self.groups(), set())


class LinkedSessionReadsAndMarksOnlyTests(APITestCase):
    """
    Covering a colleague is opening a register, marking it, and adding a
    walk-in. Editing the lesson itself belongs to the office, and a link is
    not a way to reach it.
    """

    def setUp(self):
        from apps.core.models import Branch, City
        from apps.courses.models import CourseType
        from apps.instructors.models import Instructor

        self.alice = make_user('alice7@test', first_name='אליס')
        self.bob = make_user('bob7@test', first_name='בוב')
        self.manager = make_user('manager7@test', role=UserProfile.ROLE_MANAGER)
        self.partner = make_user('partner7@test', role=UserProfile.ROLE_PARTNER)

        city = City.objects.create(name='עיר בדיקה')
        self.branch = Branch.objects.create(name='סניף בדיקה', city=city)
        self.ctype = CourseType.objects.create(name='סוג בדיקה')
        self.partner.profile.assigned_branches.add(self.branch)

        alice_instructor = Instructor.objects.create(
            first_name='אליס', last_name='מדריכה', email='alice7@test', primary_branch=self.branch
        )
        bob_instructor = Instructor.objects.create(
            first_name='בוב', last_name='מדריך', email='bob7@test', primary_branch=self.branch
        )
        self.alice_lesson = self._lesson('חוג של אליס', alice_instructor, 3)
        self.bob_lesson = self._lesson('חוג של בוב', bob_instructor, 1)
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.bob)

    def _lesson(self, name, instructor, day_of_week):
        from apps.courses.models import Course, Lesson

        course = Course.objects.create(
            name=name, branch=self.branch, course_type=self.ctype,
            price=Decimal('200.00'), capacity=12,
        )
        return Lesson.objects.create(
            course=course, instructor=instructor,
            day_of_week=day_of_week, start_time='16:00', end_time='17:00', is_recurring=True,
        )

    def auth(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def detail_url(self, lesson, as_user=None):
        suffix = f'?as_user={as_user.id}' if as_user else ''
        return f'/api/v1/scheduling/lessons/{lesson.id}/{suffix}'

    def test_linked_instructor_cannot_edit_a_colleagues_lesson(self):
        self.auth(self.alice)
        res = self.client.patch(
            self.detail_url(self.bob_lesson, as_user=self.bob),
            {'notes': 'נערך על ידי מי שאינו מלמד'},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.bob_lesson.refresh_from_db()
        self.assertNotEqual(self.bob_lesson.notes, 'נערך על ידי מי שאינו מלמד')

    def test_linked_instructor_cannot_delete_a_colleagues_lesson(self):
        from apps.courses.models import Lesson

        self.auth(self.alice)
        res = self.client.delete(self.detail_url(self.bob_lesson, as_user=self.bob))
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Lesson.objects.filter(pk=self.bob_lesson.pk).exists())

    def test_linked_instructor_can_still_read_mark_and_add_a_walkin(self):
        """Positive control: the refusals above are about writing, not the link."""
        self.auth(self.alice)

        res = self.client.get(
            f'/api/v1/scheduling/lessons/{self.bob_lesson.id}/',
            {'as_user': str(self.bob.id), 'date': '2026-09-01'},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        res = self.client.post(
            f'/api/v1/scheduling/lessons/{self.bob_lesson.id}/mark_attendance/'
            f'?as_user={self.bob.id}',
            {'date': '2026-09-01', 'attendance': []},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        res = self.client.post(
            f'/api/v1/scheduling/lessons/{self.bob_lesson.id}/add-walkin/'
            f'?as_user={self.bob.id}',
            {'date': '2026-09-01', 'first_name': 'נועם', 'last_name': 'כהן', 'phone': '0501234567'},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    def test_a_worker_cannot_edit_or_delete_their_own_lesson(self):
        from apps.courses.models import Lesson

        self.auth(self.alice)
        res = self.client.patch(
            self.detail_url(self.alice_lesson), {'notes': 'שינוי'}, format='json'
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        res = self.client.delete(self.detail_url(self.alice_lesson))
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Lesson.objects.filter(pk=self.alice_lesson.pk).exists())

    def test_a_manager_still_edits_and_deletes(self):
        from apps.courses.models import Lesson

        self.auth(self.manager)
        res = self.client.patch(
            self.detail_url(self.bob_lesson), {'notes': 'עודכן במשרד'}, format='json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.bob_lesson.refresh_from_db()
        self.assertEqual(self.bob_lesson.notes, 'עודכן במשרד')

        res = self.client.delete(self.detail_url(self.bob_lesson))
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Lesson.objects.filter(pk=self.bob_lesson.pk).exists())

    def test_a_partner_still_edits_a_lesson_in_an_assigned_branch(self):
        self.auth(self.partner)
        res = self.client.patch(
            self.detail_url(self.bob_lesson), {'notes': 'עודכן על ידי שותף'}, format='json'
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.bob_lesson.refresh_from_db()
        self.assertEqual(self.bob_lesson.notes, 'עודכן על ידי שותף')
