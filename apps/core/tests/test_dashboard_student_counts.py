"""
The dashboard's student figures, by the owner's rule (24.9.2026): a student is
a child who is פעיל or בעיה באשראי — "מי שהאשראי שלו לא עבר הוא גם פעיל".

Three figures were wrong in production on 27.9.2026:

* the students tab counted status 'active' alone, so a child whose card had
  just failed was not a student;
* the branches tab counted every child not ghost/inactive by the family's
  branch — 1,199 where the branch page headers add up to 627;
* the dropout figure counted card failures (active → payment_problem) as
  dropouts and missed departures by way of payment_problem → inactive.
"""
from datetime import date, timedelta

from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.core.models import BranchMonthlySnapshot, UserProfile
from apps.core.morning_fixes import fix_child_statuses
from apps.core.tests.test_fixtures import BaseAPITestCase, TestDataFactory
from apps.customers.status_history_models import ChildStatusHistory
from apps.enrollments.enrollment_counts import active_student_enrollments
from apps.enrollments.models import LessonEnrollment

TODAY = date.today()
TRIAL_DAY = TODAY + timedelta(days=3)


class _Students(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.other_branch = TestDataFactory.create_branch(name='סניף צפון', city=self.city)
        self.course_type = TestDataFactory.create_course_type(name='אקרובטיקה')
        self.course = TestDataFactory.create_course(branch=self.branch, course_type=self.course_type)
        self.lesson = TestDataFactory.create_lesson(course=self.course)
        self.second_lesson = TestDataFactory.create_lesson(
            course=TestDataFactory.create_course(name='מתקדמים', branch=self.branch), day_of_week=2,
        )
        self.other_lesson = TestDataFactory.create_lesson(
            course=TestDataFactory.create_course(name='צפון', branch=self.other_branch),
        )
        self.family = TestDataFactory.create_family(branch=self.branch)

    def child(self, status='active', family=None, **kwargs):
        # A name of its own: two children of one family with one name are the
        # same child to the duplicate folding.
        self._n = getattr(self, '_n', 0) + 1
        kwargs.setdefault('first_name', f'ילד{self._n}')
        return TestDataFactory.create_child(family=family or self.family, status=status, **kwargs)

    def enroll(self, child, lesson=None, **kwargs):
        return LessonEnrollment.objects.create(
            lesson=lesson or self.lesson, child=child, status=kwargs.pop('status', 'active'), **kwargs,
        )

    def header(self, branch):
        """What the branch page header shows (core/branches/<id>/statistics/)."""
        return (
            active_student_enrollments().filter(lesson__course__branch=branch)
            .values('child').distinct().count()
        )

    def students_tab(self, **params):
        res = self.client.get('/api/v1/core/dashboard/students/', params)
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def as_partner_of(self, *branches):
        partner = TestDataFactory.create_user(username='partner@example.com', role=UserProfile.ROLE_PARTNER)
        partner.profile.assigned_branches.set(branches)
        token, _ = Token.objects.get_or_create(user=partner)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')


class StudentsTabActiveStudents(_Students):
    """"תלמידים פעילים" on the students tab and the overview."""

    def test_a_child_whose_card_failed_is_still_a_student(self):
        self.enroll(self.child('active'))
        self.enroll(self.child('payment_problem'))
        self.enroll(self.child('pending'))
        self.enroll(self.child('trial_signed'), trial_lesson_date=TRIAL_DAY)
        self.child('trial_completed')
        self.enroll(self.child('inactive'), status='inactive')
        self.child('ghost')

        kpis = self.students_tab()['kpis']

        self.assertEqual(kpis['active_students'], 2)
        # The card failures keep their own tile.
        self.assertEqual(kpis['credit_problems'], 1)

    def test_limited_to_a_branch_it_is_the_branch_page_header(self):
        self.enroll(self.child('active'))
        self.enroll(self.child('payment_problem'))
        self.enroll(self.child('payment_problem'))
        # A student of the other branch, here only for a trial the office booked.
        visitor = self.child('active')
        self.enroll(visitor, lesson=self.other_lesson)
        self.enroll(visitor, trial_lesson_date=TRIAL_DAY)
        # Signed up, never paid: the row is active, the child is not a student.
        self.enroll(self.child('pending'))

        kpis = self.students_tab(branch_id=str(self.branch.id))['kpis']

        self.assertEqual(kpis['active_students'], 3)
        self.assertEqual(kpis['active_students'], self.header(self.branch))

    def test_a_partner_counts_the_students_of_their_branches(self):
        self.enroll(self.child('active'))
        self.enroll(self.child('active'), lesson=self.other_lesson)
        self.enroll(self.child('payment_problem'), lesson=self.other_lesson)
        self.as_partner_of(self.other_branch)

        kpis = self.students_tab()['kpis']

        self.assertEqual(kpis['active_students'], 2)


class BranchesTabStudentCounts(_Students):
    """"סה״כ תלמידים" and each branch's "תלמידים" on the branches tab."""

    def setUp(self):
        super().setUp()
        month = timezone.now().date().strftime('%Y-%m')
        for branch in (self.branch, self.other_branch):
            BranchMonthlySnapshot.objects.create(branch=branch, month=month)

    def tab(self, **params):
        res = self.client.get('/api/v1/core/dashboard/branches/', params)
        self.assertEqual(res.status_code, 200, res.data)
        by_branch = {row['branch_id']: row['students'] for row in res.data['branch_list']}
        return res.data['kpis']['total_students'], by_branch

    def test_counts_active_and_card_problem_children_with_a_seat_only(self):
        self.enroll(self.child('active'))
        self.enroll(self.child('payment_problem'))
        # A trial: booked on the row and on the child.
        self.enroll(self.child('trial_signed'), trial_lesson_date=TRIAL_DAY)
        # An active child whose only row here is a trial the office booked.
        self.enroll(self.child('active'), trial_lesson_date=TRIAL_DAY)
        # A trial the parent did not go on with; a sign-up that never paid.
        self.child('trial_completed')
        self.enroll(self.child('pending'))
        # Left: the row was closed. A walk-in.
        self.enroll(self.child('inactive'), status='inactive')
        self.child('ghost')

        total, by_branch = self.tab()

        self.assertEqual(total, 2)
        self.assertEqual(by_branch[str(self.branch.id)], 2)

    def test_each_branch_shows_what_its_page_header_shows(self):
        self.enroll(self.child('active'))
        self.enroll(self.child('payment_problem'))
        self.enroll(self.child('pending'))
        self.enroll(self.child('active'), lesson=self.other_lesson)
        self.enroll(self.child('active'), lesson=self.other_lesson, trial_lesson_date=TRIAL_DAY)

        _, by_branch = self.tab()

        self.assertEqual(by_branch[str(self.branch.id)], self.header(self.branch))
        self.assertEqual(by_branch[str(self.other_branch.id)], self.header(self.other_branch))
        self.assertEqual((by_branch[str(self.branch.id)], by_branch[str(self.other_branch.id)]), (2, 1))

    def test_a_child_belongs_to_the_branch_of_the_lesson_not_of_the_family(self):
        self.enroll(self.child('active'), lesson=self.other_lesson)

        total, by_branch = self.tab()

        self.assertEqual(total, 1)
        self.assertEqual(by_branch[str(self.other_branch.id)], 1)
        self.assertEqual(by_branch[str(self.branch.id)], 0)

    def test_two_lessons_in_one_branch_are_one_student(self):
        child = self.child('active')
        self.enroll(child)
        self.enroll(child, lesson=self.second_lesson)

        total, by_branch = self.tab()

        self.assertEqual(total, 1)
        self.assertEqual(by_branch[str(self.branch.id)], 1)

    def test_a_child_in_two_branches_is_in_each_but_once_in_the_total(self):
        child = self.child('active')
        self.enroll(child)
        self.enroll(child, lesson=self.other_lesson)

        total, by_branch = self.tab()

        self.assertEqual(total, 1)
        self.assertEqual(by_branch[str(self.branch.id)], 1)
        self.assertEqual(by_branch[str(self.other_branch.id)], 1)

    def test_the_branch_filter_counts_that_branch(self):
        self.enroll(self.child('active'))
        self.enroll(self.child('active'), lesson=self.other_lesson)

        total, _ = self.tab(branch_id=str(self.other_branch.id))

        self.assertEqual(total, 1)

    def test_a_partner_sees_only_the_children_of_their_branches(self):
        self.enroll(self.child('active'))
        self.enroll(self.child('active'), lesson=self.other_lesson)
        self.enroll(self.child('payment_problem'), lesson=self.other_lesson)
        self.as_partner_of(self.other_branch)

        total, by_branch = self.tab()

        self.assertEqual(total, 2)
        self.assertEqual(set(by_branch), {str(self.other_branch.id)})


class Dropout(_Students):
    """"נושרים": children who moved to לא פעיל in the window, once each."""

    WINDOW = {
        'quit_date_from': '2020-01-01',
        # A day of slack either side of midnight in Israel.
        'quit_date_to': (TODAY + timedelta(days=1)).isoformat(),
    }

    def quit(self, **params):
        return self.students_tab(**{**self.WINDOW, **params})['quit_percentage']

    def history(self, child, was, now, when=None):
        return ChildStatusHistory.objects.create(
            child=child, previous_status=was, new_status=now, changed_at=when or timezone.now(),
        )

    def test_a_card_failure_is_not_a_dropout(self):
        # Card failed, then replaced: the save flow records the failure only.
        child = self.child('active')
        self.enroll(child)
        child.status = 'payment_problem'
        child.save()
        child.status = 'active'
        child.save()
        # One still open.
        still_open = self.child('active')
        self.enroll(still_open)
        still_open.status = 'payment_problem'
        still_open.save()

        quit = self.quit()

        self.assertEqual(quit['total_quit'], 0)
        self.assertEqual(quit['by_status'], [])
        self.assertEqual(quit['by_previous_status'], [])
        self.assertEqual(self.students_tab()['kpis']['credit_problems'], 1)

    def test_leaving_after_a_card_failure_is_a_dropout(self):
        child = self.child('active')
        self.enroll(child, status='inactive')
        child.status = 'payment_problem'
        child.save()
        child.status = 'inactive'
        child.save()

        quit = self.quit()

        self.assertEqual(quit['total_quit'], 1)
        self.assertEqual([row['status_key'] for row in quit['by_status']], ['inactive'])
        self.assertEqual(
            [(row['status_key'], row['count']) for row in quit['by_previous_status']],
            [('payment_problem', 1)],
        )
        self.assertEqual(quit['by_previous_status'][0]['children'][0]['id'], str(child.id))

    def test_the_save_signal_records_a_move_to_inactive_from_any_status(self):
        child = self.child('payment_problem')
        child.status = 'inactive'
        child.save()

        rows = list(ChildStatusHistory.objects.filter(child=child).values_list('previous_status', 'new_status'))

        self.assertEqual(rows, [('payment_problem', 'inactive')])

    def test_the_morning_fix_and_the_signal_count_one_child_once(self):
        # The paid period ended and nothing is left: the morning routine moves
        # the child to לא פעיל and writes a row beside the signal's.
        child = self.child('payment_problem', paid_until_date=TODAY - timedelta(days=20))
        self.enroll(child, status='inactive')

        fix_child_statuses()

        child.refresh_from_db()
        self.assertEqual(child.status, 'inactive')
        self.assertEqual(ChildStatusHistory.objects.filter(child=child, new_status='inactive').count(), 2)
        quit = self.quit()
        self.assertEqual(quit['total_quit'], 1)
        self.assertEqual(quit['by_status'][0]['count'], 1)
        self.assertEqual(quit['by_course_type'], [
            {'course_type_id': str(self.course_type.id), 'course_type_name': 'אקרובטיקה', 'count': 1},
        ])

    def test_left_came_back_and_left_again_is_one_child(self):
        child = self.child('inactive')
        self.enroll(child, status='inactive')
        self.history(child, 'active', 'inactive', timezone.now() - timedelta(days=10))
        self.history(child, 'inactive', 'active', timezone.now() - timedelta(days=5))
        self.history(child, 'payment_problem', 'inactive', timezone.now() - timedelta(days=1))

        quit = self.quit()

        self.assertEqual(quit['total_quit'], 1)
        # Counted under the latest departure.
        self.assertEqual([row['status_key'] for row in quit['by_previous_status']], ['payment_problem'])

    def test_only_departures_inside_the_window(self):
        early = self.child('inactive')
        self.history(early, 'active', 'inactive', timezone.now() - timedelta(days=120))
        recent = self.child('inactive')
        self.history(recent, 'active', 'inactive', timezone.now() - timedelta(days=2))

        quit = self.quit(
            quit_date_from=(TODAY - timedelta(days=30)).isoformat(),
            quit_date_to=(TODAY + timedelta(days=1)).isoformat(),
        )

        self.assertEqual(quit['total_quit'], 1)
        self.assertEqual(quit['by_status'][0]['children'][0]['id'], str(recent.id))

    def test_a_legacy_non_active_row_is_a_departure(self):
        child = self.child('inactive')
        self.history(child, 'active', 'non_active')

        quit = self.quit()

        self.assertEqual(quit['total_quit'], 1)
        self.assertEqual(quit['by_status'][0]['status_key'], 'inactive')
        self.assertEqual(quit['by_status'][0]['status'], 'לא פעיל')
