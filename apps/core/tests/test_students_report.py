"""
The students report (דוח תלמידים, owner 14.10.2026): one answer with, for the
chosen branches together and for each one, the students on every day of the
period and how they sit today — by weekday, by instructor, and group by group
against capacity.

What must hold:
* the last point is the figure the students tab shows for the same branch —
  same rule (פעיל / בעיה באשראי, a regular place), same children;
* a child in two lessons is one student of the branch, and a student of each
  day, instructor and group;
* the days before today follow the places taken and closed and the status
  changes the child went through;
* a group's capacity is the tightest of lesson, group and room, and the
  weakest group comes first;
* a partner gets their own branches only, whatever is asked for.
"""
from datetime import date, datetime, time, timedelta

from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.core.models import UserProfile
from apps.core.students_report import place_span, sample_days, student_spans
from apps.core.tests.test_fixtures import BaseAPITestCase, TestDataFactory
from apps.customers.models import Child
from apps.customers.status_history_models import ChildStatusHistory
from apps.enrollments.models import LessonEnrollment

TODAY = timezone.localdate()
URL = '/api/v1/core/dashboard/students-report/'


def ago(days):
    return TODAY - timedelta(days=days)


class _Report(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.other_branch = TestDataFactory.create_branch(name='סניף צפון', city=self.city)
        self.instructor = TestDataFactory.create_instructor(first_name='נועה', last_name='לוי', branch=self.branch)
        self.room = TestDataFactory.create_room(branch=self.branch, capacity=19)
        self.course = TestDataFactory.create_course(name='אקרובטיקה', branch=self.branch, capacity=20)
        # Sunday: room 19, group 20 → 19.
        self.sunday = TestDataFactory.create_lesson(
            course=self.course, room=self.room, instructor=self.instructor, day_of_week=0,
        )
        # Tuesday: the lesson itself is limited to 6.
        self.tuesday = TestDataFactory.create_lesson(
            course=self.course, room=self.room, instructor=self.instructor, day_of_week=2, capacity=6,
        )
        self.north = TestDataFactory.create_lesson(
            course=TestDataFactory.create_course(name='צפון', branch=self.other_branch), day_of_week=3,
        )
        self.family = TestDataFactory.create_family(branch=self.branch)

    def child(self, status='active'):
        self._n = getattr(self, '_n', 0) + 1
        return TestDataFactory.create_child(family=self.family, status=status, first_name=f'ילד{self._n}')

    def enroll(self, child, lesson=None, *, since=None, status='active', **kwargs):
        row = LessonEnrollment.objects.create(
            lesson=lesson or self.sunday, child=child, status=status,
            start_date=since or ago(60), **kwargs,
        )
        LessonEnrollment.objects.filter(pk=row.pk).update(
            enrolled_at=timezone.make_aware(datetime.combine(since or ago(60), time(12))),
        )
        return row

    def status_change(self, child, previous, new, days_ago):
        ChildStatusHistory.objects.create(
            child=child, previous_status=previous, new_status=new,
            changed_at=timezone.make_aware(datetime.combine(ago(days_ago), time(10))),
        )
        Child.objects.filter(pk=child.pk).update(status=new)

    def report(self, branches=None, date_from=None, date_to=None, expect=200):
        params = {
            'date_from': (date_from or ago(30)).isoformat(),
            'date_to': (date_to or TODAY).isoformat(),
            'branches': branches if isinstance(branches, str) else ','.join(
                str(b.id) for b in (branches or [self.branch])
            ),
        }
        res = self.client.get(URL, params)
        self.assertEqual(res.status_code, expect, getattr(res, 'data', None))
        return res.data

    def students_tab(self, branch):
        res = self.client.get('/api/v1/core/dashboard/students/', {'branch_id': str(branch.id)})
        self.assertEqual(res.status_code, 200)
        return res.data['kpis']['active_students']

    def page(self, data, branch):
        return next(p for p in data['branches'] if p['branch_id'] == str(branch.id))


class TodayIsTheStudentsTab(_Report):
    def test_the_last_point_is_the_students_tab_figure(self):
        self.enroll(self.child('active'))
        self.enroll(self.child('payment_problem'))
        # In both lessons of the branch: one student.
        both = self.child('active')
        self.enroll(both)
        self.enroll(both, lesson=self.tuesday)
        # Not students: signed up and never paid, a trial, one who left.
        self.enroll(self.child('pending'))
        self.enroll(self.child('trial_signed'), trial_lesson_date=TODAY + timedelta(days=2))
        self.enroll(self.child('inactive'), status='inactive', end_date=ago(40))
        # A student of the other branch only.
        self.enroll(self.child('active'), lesson=self.north)

        data = self.report()
        page = self.page(data, self.branch)

        self.assertEqual(page['students'], 3)
        self.assertEqual(page['students'], self.students_tab(self.branch))
        self.assertEqual(page['series'][-1], {'date': TODAY.isoformat(), 'students': 3})
        self.assertEqual(data['summary']['students'], 3)

    def test_a_place_dated_to_start_next_week_counts_today_as_on_the_tab(self):
        row = self.enroll(self.child('active'))
        LessonEnrollment.objects.filter(pk=row.pk).update(
            start_date=TODAY + timedelta(days=7), enrolled_at=timezone.now(),
        )

        page = self.page(self.report(), self.branch)

        self.assertEqual(page['students'], 1)
        self.assertEqual(page['students'], self.students_tab(self.branch))

    def test_a_date_after_today_is_read_as_today(self):
        self.enroll(self.child('active'))

        data = self.report(date_to=TODAY + timedelta(days=20))

        self.assertEqual(data['date_to'], TODAY.isoformat())
        self.assertEqual(data['requested_date_to'], (TODAY + timedelta(days=20)).isoformat())
        self.assertEqual(data['summary']['series'][-1]['date'], TODAY.isoformat())


class OneChildTwoLessons(_Report):
    def test_once_in_the_branch_and_once_in_each_day_group_and_instructor(self):
        both = self.child('active')
        self.enroll(both)
        self.enroll(both, lesson=self.tuesday)
        self.enroll(self.child('active'))

        page = self.page(self.report(), self.branch)

        self.assertEqual(page['students'], 2)
        days = {row['label']: row['students'] for row in page['by_weekday']}
        self.assertEqual(days, {'ראשון': 2, 'שלישי': 1})
        groups = {g['lesson_id']: g['students'] for g in page['groups']}
        self.assertEqual(groups[str(self.sunday.id)], 2)
        self.assertEqual(groups[str(self.tuesday.id)], 1)
        # Same instructor for both lessons: two children, not three.
        teacher = next(t for t in page['by_instructor'] if t['name'] == 'נועה לוי')
        self.assertEqual(teacher['students'], 2)
        self.assertEqual(teacher['seats'], 3)
        self.assertEqual(teacher['groups'], 2)

    def test_a_child_in_two_branches_is_one_student_of_the_two_together(self):
        both = self.child('active')
        self.enroll(both)
        self.enroll(both, lesson=self.north)

        data = self.report(branches=[self.branch, self.other_branch])

        self.assertEqual(data['summary']['students'], 1)
        self.assertEqual(self.page(data, self.branch)['students'], 1)
        self.assertEqual(self.page(data, self.other_branch)['students'], 1)


class TheDaysBeforeToday(_Report):
    def test_places_taken_and_closed_and_children_who_left(self):
        # Here all along.
        self.enroll(self.child('active'), since=ago(90))
        # Joined ten days ago.
        self.enroll(self.child('active'), since=ago(10))
        # Cancelled the course five days ago: the place closed that day, and
        # with nothing left the child became לא פעיל.
        cancelled = self.child('active')
        self.enroll(cancelled, since=ago(90), status='inactive', end_date=ago(5))
        self.status_change(cancelled, 'active', 'inactive', 5)
        # Left three days ago by status; the place was never closed.
        gone = self.child('active')
        self.enroll(gone, since=ago(90))
        self.status_change(gone, 'active', 'inactive', 3)

        page = self.page(self.report(date_from=ago(20)), self.branch)
        by_day = {row['date']: row['students'] for row in page['series']}

        self.assertEqual(by_day[ago(20).isoformat()], 3)
        self.assertEqual(by_day[ago(10).isoformat()], 4)
        self.assertEqual(by_day[ago(6).isoformat()], 4)
        self.assertEqual(by_day[ago(5).isoformat()], 3)
        self.assertEqual(by_day[ago(3).isoformat()], 2)
        self.assertEqual(by_day[TODAY.isoformat()], 2)
        self.assertEqual(page['students_at_start'], 3)
        self.assertEqual(page['joined'], 1)
        self.assertEqual(page['left'], 2)

    def test_a_card_failure_is_still_a_student_on_those_days(self):
        child = self.child('active')
        self.enroll(child, since=ago(90))
        self.status_change(child, 'active', 'payment_problem', 8)
        self.status_change(child, 'payment_problem', 'inactive', 2)

        page = self.page(self.report(date_from=ago(10)), self.branch)
        by_day = {row['date']: row['students'] for row in page['series']}

        self.assertEqual(by_day[ago(10).isoformat()], 1)
        self.assertEqual(by_day[ago(5).isoformat()], 1)
        self.assertEqual(by_day[ago(2).isoformat()], 0)

    def test_a_trial_that_became_a_place_counts_from_the_trial(self):
        child = self.child('active')
        row = self.enroll(child, since=ago(20))
        LessonEnrollment.objects.filter(pk=row.pk).update(start_date=ago(12), trial_held_on=ago(12))

        page = self.page(self.report(date_from=ago(20)), self.branch)
        by_day = {r['date']: r['students'] for r in page['series']}

        self.assertEqual(by_day[ago(13).isoformat()], 0)
        self.assertEqual(by_day[ago(12).isoformat()], 1)


class GroupsAgainstCapacity(_Report):
    def test_tightest_capacity_missing_places_and_weakest_first(self):
        for _ in range(4):
            self.enroll(self.child('active'))  # Sunday: 4 of 19
        for _ in range(5):
            self.enroll(self.child('active'), lesson=self.tuesday)  # Tuesday: 5 of 6
        empty = TestDataFactory.create_lesson(
            course=self.course, room=self.room, instructor=self.instructor, day_of_week=4,
        )

        page = self.page(self.report(), self.branch)
        groups = page['groups']

        self.assertEqual([g['lesson_id'] for g in groups], [str(empty.id), str(self.sunday.id), str(self.tuesday.id)])
        sunday = groups[1]
        self.assertEqual((sunday['students'], sunday['capacity'], sunday['missing']), (4, 19, 15))
        self.assertEqual((groups[2]['capacity'], groups[2]['missing']), (6, 1))
        self.assertEqual((groups[0]['students'], groups[0]['missing']), (0, 19))
        occupancy = page['occupancy']
        self.assertEqual(occupancy['seats'], 9)
        self.assertEqual(occupancy['capacity'], 44)
        self.assertEqual(occupancy['missing'], 35)
        self.assertEqual(occupancy['weak_groups'], 2)

    def test_listed_by_field_and_weakest_first_within_it(self):
        """Owner, 14.10.2026: the groups are listed by field (תחום)."""
        acro = TestDataFactory.create_course(
            name='אקרו מתקדמים', branch=self.branch, course_type=TestDataFactory.create_course_type('אקרובטיקה'),
        )
        full = TestDataFactory.create_lesson(course=acro, room=self.room, instructor=self.instructor, day_of_week=1)
        for _ in range(10):
            self.enroll(self.child('active'), lesson=full)
        empty_acro = TestDataFactory.create_lesson(course=acro, room=self.room, instructor=self.instructor, day_of_week=3)
        for _ in range(4):
            self.enroll(self.child('active'))  # Sunday, the other field

        groups = self.page(self.report(), self.branch)['groups']

        self.assertEqual(
            [(g['field_name'], g['lesson_id']) for g in groups],
            [
                ('אקרובטיקה', str(empty_acro.id)),
                ('אקרובטיקה', str(full.id)),
                (self.course.course_type.name, str(self.tuesday.id)),
                (self.course.course_type.name, str(self.sunday.id)),
            ],
        )

    def test_a_cancelled_or_one_off_lesson_with_nobody_is_not_listed(self):
        TestDataFactory.create_lesson(course=self.course, day_of_week=5, status='cancelled')
        TestDataFactory.create_lesson(course=self.course, day_of_week=6, is_recurring=False, lesson_date=TODAY)

        page = self.page(self.report(), self.branch)

        self.assertEqual({g['day'] for g in page['groups']}, {0, 2})


class WhoMayAsk(_Report):
    def as_partner_of(self, *branches):
        partner = TestDataFactory.create_user(username='partner@example.com', role=UserProfile.ROLE_PARTNER)
        partner.profile.assigned_branches.set(branches)
        token, _ = Token.objects.get_or_create(user=partner)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_a_partner_gets_their_own_branches_only(self):
        self.enroll(self.child('active'))
        self.enroll(self.child('active'), lesson=self.north)
        self.as_partner_of(self.other_branch)

        data = self.report(branches=[self.branch, self.other_branch])
        self.assertEqual([p['branch_id'] for p in data['branches']], [str(self.other_branch.id)])
        self.assertEqual(data['summary']['students'], 1)

        everything = self.report(branches='all')
        self.assertEqual([p['branch_id'] for p in everything['branches']], [str(self.other_branch.id)])
        self.assertIsNone(everything['students_tab_total'])

        self.report(branches=[self.branch], expect=400)

    def test_an_instructor_is_refused(self):
        worker = TestDataFactory.create_user(username='worker@example.com', role=UserProfile.ROLE_WORKER)
        token, _ = Token.objects.get_or_create(user=worker)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

        self.report(expect=403)

    def test_all_leaves_out_the_external_branches_and_says_the_tab_total(self):
        external = TestDataFactory.create_branch(name='עירייה', city=self.city, is_external=True)
        self.enroll(self.child('active'))
        self.child('active')  # פעיל, with no place anywhere

        data = self.report(branches='all')

        self.assertNotIn(str(external.id), [p['branch_id'] for p in data['branches']])
        self.assertTrue(data['all_branches'])
        self.assertEqual(data['summary']['students'], 1)
        self.assertEqual(data['students_tab_total'], 2)

    def test_bad_requests(self):
        self.report(branches='', expect=400)
        self.report(branches='not-an-id', expect=400)
        self.report(date_from=TODAY + timedelta(days=3), date_to=TODAY + timedelta(days=9), expect=400)
        res = self.client.get(URL, {'branches': 'all'})
        self.assertEqual(res.status_code, 400)


class Pieces(_Report):
    def test_status_read_backwards_from_today(self):
        lo, hi = date(2026, 9, 1), date(2026, 9, 30)
        # Card failed on the 10th, left on the 20th.
        changes = [(date(2026, 9, 10), 'active'), (date(2026, 9, 20), 'payment_problem')]
        self.assertEqual(student_spans('inactive', changes, lo, hi), [(lo, date(2026, 9, 20))])
        # Came back afterwards without a line in the history (a move into פעיל
        # is often not written): read from today, they count from the 20th on.
        # The place they hold decides the rest — a new place starts the day it
        # was taken.
        self.assertEqual(student_spans('active', changes, lo, hi), [(lo, hi)])
        # Two changes on one day: the first says what the day before was.
        spans = student_spans('inactive', [(date(2026, 9, 5), 'active'), (date(2026, 9, 5), 'payment_problem')], lo, hi)
        self.assertEqual(spans, [(lo, date(2026, 9, 5))])

    def test_place_span(self):
        today = date(2026, 10, 14)
        row = {
            'status': 'inactive', 'start_date': date(2026, 9, 1), 'end_date': None,
            'enrolled_at': None, 'updated_at': timezone.make_aware(datetime(2026, 10, 2, 9)),
            'trial_held_on': None,
        }
        self.assertEqual(place_span(row, today), (date(2026, 9, 1), date(2026, 10, 2)))
        row.update(status='active', end_date=date(2026, 9, 3))
        self.assertEqual(place_span(row, today), (date(2026, 9, 1), None))

    def test_sample_days_end_on_the_last_day(self):
        days = sample_days(date(2024, 1, 1), date(2026, 10, 14))
        self.assertLessEqual(len(days), 402)
        self.assertEqual(days[-1], date(2026, 10, 14))
        self.assertEqual(len(sample_days(date(2026, 10, 1), date(2026, 10, 14))), 14)
