"""
The overall summary of an instructor who may open colleagues' accounts.

Owner, 13.10.2026: "אני צריך שלמדריך שיש לו הרשאה לעוד מורים יכול לראות סיכום
כולל." Until now such an instructor could only switch between the accounts one
at a time; nothing added them up.

`as_user=all` runs the dashboard and the trials list over the caller and every
account linked to them — each inside the branch its own link is limited to, so
adding up never reaches further than switching did.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, LinkedUserAccess, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.models import LessonEnrollment
from apps.instructors.models import Instructor

User = get_user_model()


def make_user(username, role=UserProfile.ROLE_WORKER, **extra):
    user = User.objects.create_user(username=username, email=username, password='pw-for-tests', **extra)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return user


class OverallSummaryTests(APITestCase):
    def setUp(self):
        city = City.objects.create(name='עיר')
        self.north = Branch.objects.create(name='סניף צפון', city=city)
        self.south = Branch.objects.create(name='סניף דרום', city=city)
        self.ctype = CourseType.objects.create(name='קפוארה')

        self.head = make_user('head@sum.test', first_name='ראש', last_name='צוות')
        self.bob = make_user('bob@sum.test', first_name='בוב', last_name='מדריך')
        self.carol = make_user('carol@sum.test', first_name='קרול', last_name='מדריכה')
        self.stranger = make_user('dan@sum.test', first_name='דן', last_name='זר')

        self.head_lesson = self.lesson(self.teacher(self.head, self.north), self.north, 'של ראש הצוות', 1)
        bob = self.teacher(self.bob, self.north)
        self.bob_north = self.lesson(bob, self.north, 'בוב צפון', 2)
        self.bob_south = self.lesson(bob, self.south, 'בוב דרום', 3)
        self.carol_lesson = self.lesson(self.teacher(self.carol, self.south), self.south, 'של קרול', 4)
        self.stranger_lesson = self.lesson(self.teacher(self.stranger, self.north), self.north, 'של זר', 5)

        for index in range(2):
            self.student(self.head_lesson, f'א{index}')
        for index in range(3):
            self.student(self.bob_north, f'ב{index}')
        self.student(self.bob_south, 'בד')
        for index in range(4):
            self.student(self.carol_lesson, f'ק{index}')
        self.student(self.stranger_lesson, 'ז')

        self.url = reverse('instructor-my-dashboard')
        token, _ = Token.objects.get_or_create(user=self.head)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def teacher(self, user, branch):
        return Instructor.objects.create(
            first_name=user.first_name, last_name=user.last_name, email=user.email, primary_branch=branch,
        )

    def lesson(self, instructor, branch, name, day_of_week):
        course = Course.objects.create(
            name=name, branch=branch, course_type=self.ctype, price=Decimal('200.00'), capacity=20,
            instructor=instructor,
        )
        return Lesson.objects.create(
            course=course, instructor=instructor, day_of_week=day_of_week,
            start_time='16:00', end_time='17:00', is_recurring=True,
        )

    def student(self, lesson, name, child=None):
        if child is None:
            family = Family.objects.create(name=name, phone=f'05{abs(hash(name)) % 10**8:08d}')
            child = Child.objects.create(
                family=family, first_name=name, last_name='כהן',
                birth_date=date(2016, 4, 4), gender='male', status='active',
            )
        LessonEnrollment.objects.create(lesson=lesson, child=child, status='active')
        return child

    def overall(self, **params):
        res = self.client.get(self.url, {'as_user': 'all', **params})
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def test_with_no_links_it_is_the_instructors_own_numbers(self):
        data = self.overall()
        self.assertEqual(data['total_active_students'], 2)
        self.assertEqual([row['name'] for row in data['teachers']], ['ראש צוות'])
        self.assertTrue(data['subject']['is_all'])

    def test_it_adds_up_the_caller_and_every_linked_colleague(self):
        LinkedUserAccess.objects.create(owner=self.head, linked=self.bob)
        LinkedUserAccess.objects.create(owner=self.head, linked=self.carol)
        data = self.overall()
        self.assertEqual(data['total_active_students'], 2 + 4 + 4)
        self.assertEqual(
            {row['name']: (row['active_students'], row['groups']) for row in data['teachers']},
            {'ראש צוות': (2, 1), 'בוב מדריך': (4, 2), 'קרול מדריכה': (4, 1)},
        )
        self.assertEqual(
            {group['course_name']: group['instructor_name'] for group in data['groups']},
            {'של ראש הצוות': 'ראש צוות', 'בוב צפון': 'בוב מדריך', 'בוב דרום': 'בוב מדריך', 'של קרול': 'קרול מדריכה'},
        )

    def test_a_colleague_nobody_linked_is_not_in_it(self):
        LinkedUserAccess.objects.create(owner=self.head, linked=self.bob)
        data = self.overall()
        self.assertNotIn('של זר', {group['course_name'] for group in data['groups']})
        self.assertNotIn('של קרול', {group['course_name'] for group in data['groups']})
        self.assertEqual(data['total_active_students'], 2 + 4)

    def test_a_link_limited_to_one_branch_adds_up_that_branch_only(self):
        LinkedUserAccess.objects.create(owner=self.head, linked=self.bob, branch=self.north)
        data = self.overall()
        self.assertEqual({group['course_name'] for group in data['groups']}, {'של ראש הצוות', 'בוב צפון'})
        self.assertEqual(data['total_active_students'], 2 + 3)
        bob = next(row for row in data['teachers'] if row['name'] == 'בוב מדריך')
        self.assertEqual((bob['active_students'], bob['groups']), (3, 1))

    def test_a_child_two_teachers_both_teach_is_one_student_in_the_total(self):
        LinkedUserAccess.objects.create(owner=self.head, linked=self.bob)
        shared = Child.objects.filter(first_name='א0').get()
        self.student(self.bob_north, 'א0', child=shared)
        data = self.overall()
        self.assertEqual(data['total_active_students'], 2 + 4)
        lines = {row['name']: row['active_students'] for row in data['teachers']}
        self.assertEqual(lines, {'ראש צוות': 2, 'בוב מדריך': 5})

    def test_one_colleague_at_a_time_still_works_as_before(self):
        LinkedUserAccess.objects.create(owner=self.head, linked=self.bob)
        res = self.client.get(self.url, {'as_user': str(self.bob.id)})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['total_active_students'], 4)
        self.assertEqual(res.data['teachers'], [])
        self.assertFalse(res.data['subject']['is_all'])
        own = self.client.get(self.url)
        self.assertEqual(own.data['total_active_students'], 2)

    def test_the_branch_filter_narrows_the_whole_summary(self):
        LinkedUserAccess.objects.create(owner=self.head, linked=self.bob)
        LinkedUserAccess.objects.create(owner=self.head, linked=self.carol)
        data = self.overall(branch_id=str(self.south.id))
        self.assertEqual({group['course_name'] for group in data['groups']}, {'בוב דרום', 'של קרול'})
        self.assertEqual(data['total_active_students'], 1 + 4)

    def test_the_trials_list_runs_over_everyone_too(self):
        LinkedUserAccess.objects.create(owner=self.head, linked=self.bob)
        today = date.today()
        for lesson, name in ((self.head_lesson, 'שלי'), (self.bob_north, 'של בוב'), (self.stranger_lesson, 'של זר')):
            family = Family.objects.create(name=name, phone=f'05{abs(hash(name)) % 10**8:08d}')
            child = Child.objects.create(
                family=family, first_name=name, last_name='ניסיון',
                birth_date=date(2016, 4, 4), gender='male', status='trial_completed',
            )
            LessonEnrollment.objects.create(
                lesson=lesson, child=child, status='inactive',
                trial_lesson_date=today - timedelta(days=7), trial_outcome='attended',
            )
        res = self.client.get(reverse('instructor-my-trials'), {'as_user': 'all'})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            {(row['child_name'].split(' ניסיון')[0], row['instructor_name']) for row in res.data['trials']},
            {('שלי', 'ראש צוות'), ('של בוב', 'בוב מדריך')},
        )
        mine = self.client.get(reverse('instructor-my-trials'))
        self.assertEqual(mine.data['total'], 1)
