"""
One number of students for a lesson, on every screen.

Owner, 13.10.2026: "כמות התלמידים שמופיעים ביומן במערכת של ה-admin לא תואמים
למשתמשים של המורים… זה חמור וחייב שתוודא שכל המערכת מסונכרנת ומה נכון."

Four figures were on show for one lesson. The office's calendar drew the seats
taken (no trial statuses, no municipality children). The instructor's day drew
everyone on the register who was not a trial that day. The lesson window drew
the length of the register. Pay and the catalogue counted by the owner's own
rule of 24.9.2026 — a student is a child who is פעיל or בעיית תשלום.

`register_kind` says what each row on a register is, the schedule counts by it,
and the register sends it with every row.

The same day he said who is registered, for the register the instructors call
out: "כשאני אומר רשומים, אני מתכוון גם לכאלה שהתשלום שלהם לא עבר, וכל מיני כאלה…
ילד שביטל ועדיין אמור להגיע לסוף החודש — גם הוא רשום. אלא אם כן בוצע לו זיכוי
באותו היום על כל החודש." So a sign-up not paid yet is registered, and so is a
child who cancelled, to the end of the month they paid for. Pay and the
catalogue go on counting who pays (24.9.2026) — a different question.
"""
from datetime import date, time, timedelta
from decimal import Decimal

from rest_framework.authtoken.models import Token

from apps.core.models import UserProfile
from apps.courses.models import Lesson
from apps.customers.models import Child, Family
from apps.enrollments.enrollment_counts import (
    ON_REGISTER_OTHER,
    ON_REGISTER_STUDENT,
    ON_REGISTER_TRIAL,
    register_kind,
)
from apps.enrollments.models import LessonEnrollment
from apps.enrollments.tests.test_headcounts_exclude_trials import HeadcountTestBase, User
from apps.external_students.models import ExternalStudent


class OneStudentCountTests(HeadcountTestBase):
    def setUp(self):
        super().setUp()
        self.wednesday = date.today() + timedelta(days=(2 - date.today().weekday()) % 7 or 7)
        for i in range(3):
            self.enroll(f'פעיל{i}')
        self.enroll('כרטיס נכשל', child_status='payment_problem')
        self.enroll('לא שילם עדיין', child_status='pending')
        self.enroll('עזב ולא נסגר', child_status='inactive')
        self.enroll('ניסיון ישן בלי תאריך', child_status='trial_completed')
        self.enroll('ניסיון היום', child_status='trial_signed', trial_on=self.wednesday)
        self.enroll('ניסיון בשבוע הבא', child_status='trial_signed', trial_on=self.wednesday + timedelta(days=7))

    def calendar_row(self, client=None):
        res = (client or self.client).get(
            '/api/v1/scheduling/lessons/',
            {'start_date': self.wednesday.isoformat(), 'end_date': self.wednesday.isoformat()},
        )
        self.assertEqual(res.status_code, 200, res.data)
        return next(item for item in res.data if item['id'] == str(self.lesson.id))

    def register(self, client=None):
        res = (client or self.client).get(
            f'/api/v1/scheduling/lessons/{self.lesson.id}/', {'date': self.wednesday.isoformat()},
        )
        self.assertEqual(res.status_code, 200, res.data)
        return res.data['enrollments']

    def kinds(self, rows):
        return {kind: sum(1 for row in rows if row['register_kind'] == kind)
                for kind in (ON_REGISTER_STUDENT, ON_REGISTER_TRIAL, ON_REGISTER_OTHER)}

    def test_the_registered_are_the_active_the_failed_cards_and_the_not_yet_paid(self):
        row = self.calendar_row()
        self.assertEqual(row['active_student_count'], 5)
        self.assertEqual(row['trial_student_count'], 1)
        # The child who left and the old trial with no date are not on the register at all.
        self.assertEqual(row['other_student_count'], 0)
        self.assertEqual(row['student_count'], 6)

    def test_the_register_carries_the_same_split_row_by_row(self):
        row = self.calendar_row()
        kinds = self.kinds(self.register())
        self.assertEqual(kinds[ON_REGISTER_STUDENT], row['active_student_count'])
        self.assertEqual(kinds[ON_REGISTER_TRIAL], row['trial_student_count'])
        self.assertEqual(kinds[ON_REGISTER_OTHER], row['other_student_count'])
        self.assertEqual(sum(kinds.values()), row['student_count'])

    def test_a_child_who_left_and_an_old_trial_are_not_on_the_register(self):
        # Owner, 13.10.2026: "חייב לתקן את זה שלא יופיע, רק מי שפעיל או עם החרגה
        # ספציפית… ילד שהיה בשיעור ניסיון אמור להיות רק בשיעור הספציפי הזה."
        names = {entry['child_name'].split(' כהן')[0] for entry in self.register()}
        self.assertIn('לא שילם עדיין', names)
        self.assertIn('כרטיס נכשל', names)
        self.assertIn('ניסיון היום', names)
        self.assertNotIn('עזב ולא נסגר', names)
        self.assertNotIn('ניסיון ישן בלי תאריך', names)
        self.assertNotIn('ניסיון בשבוע הבא', names)

    def test_the_office_is_handed_the_places_nobody_closed(self):
        res = self.client.get(
            f'/api/v1/scheduling/lessons/{self.lesson.id}/', {'date': self.wednesday.isoformat()},
        )
        stale = {entry['child_name'].split(' כהן')[0]: entry['child_status'] for entry in res.data['stale_places']}
        self.assertEqual(stale, {'עזב ולא נסגר': 'inactive', 'ניסיון ישן בלי תאריך': 'trial_completed'})

    def test_a_walk_in_the_instructor_added_is_still_on_the_register(self):
        res = self.client.post(
            f'/api/v1/scheduling/lessons/{self.lesson.id}/add-walkin/',
            {'date': self.wednesday.isoformat(), 'first_name': 'מזדמן', 'last_name': 'חדש', 'phone': ''},
            format='json',
        )
        self.assertIn(res.status_code, (200, 201), res.data)
        names = {entry['child_name'] for entry in self.register()}
        self.assertTrue(any(name.startswith('מזדמן') for name in names), names)

    def test_the_instructor_reads_the_very_numbers_the_office_reads(self):
        teacher = User.objects.create_user(username='t@counts.test', email='t@counts.test', password='pw-for-tests')
        profile, _ = UserProfile.objects.get_or_create(user=teacher)
        profile.role = UserProfile.ROLE_WORKER
        profile.save(update_fields=['role'])
        office = self.calendar_row()
        office_register = self.kinds(self.register())

        token, _ = Token.objects.get_or_create(user=teacher)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        theirs = self.calendar_row()
        for field in ('active_student_count', 'trial_student_count', 'other_student_count', 'student_count', 'enrollment_count'):
            self.assertEqual(theirs[field], office[field], field)
        self.assertEqual(self.kinds(self.register()), office_register)

    def test_a_child_registered_under_two_cards_is_one_student_on_the_calendar_too(self):
        # The register already folded the two cards into one row; the calendar counted both.
        for _ in range(2):
            family = Family.objects.create(name='כפול', phone='0541112222', branch=self.branch)
            child = Child.objects.create(
                family=family, first_name='נועה', last_name='כפול',
                birth_date=date(2016, 4, 4), gender='female', status='active',
            )
            LessonEnrollment.objects.create(lesson=self.lesson, child=child, status='active')
        row = self.calendar_row()
        self.assertEqual(row['active_student_count'], 6)
        self.assertEqual(self.kinds(self.register())[ON_REGISTER_STUDENT], 6)

    def test_the_municipalitys_children_are_students_of_the_lesson(self):
        self.branch.is_external = True
        self.branch.save(update_fields=['is_external'])
        ExternalStudent.objects.create(lesson=self.lesson, first_name='ילד', last_name='עירייה', is_active=True)
        row = self.calendar_row()
        self.assertEqual(row['active_student_count'], 6)
        self.assertEqual(row['external_student_count'], 1)
        self.assertEqual(self.kinds(self.register())[ON_REGISTER_STUDENT], 6)

    def test_the_seats_taken_stay_what_registration_checks(self):
        # Capacity is its own question (a sign-up waiting for its payment holds
        # its place) and is not the number of students.
        self.assertEqual(self.calendar_row()['enrollment_count'], 6)


class RegisterKindTests(HeadcountTestBase):
    def test_a_trial_booked_for_the_date_is_a_trial_even_for_a_student_of_another_course(self):
        on = date(2026, 10, 14)
        self.assertEqual(register_kind(self.enroll('ותיקה', child_status='active', trial_on=on), on), ON_REGISTER_TRIAL)

    def test_a_trial_booked_for_another_date_is_not_a_student_here(self):
        on = date(2026, 10, 14)
        row = self.enroll('אחר', child_status='active', trial_on=on + timedelta(days=7))
        self.assertEqual(register_kind(row, on), ON_REGISTER_OTHER)

    def test_each_child_status_on_a_regular_place(self):
        on = date(2026, 10, 14)
        expected = {
            'active': ON_REGISTER_STUDENT,
            'payment_problem': ON_REGISTER_STUDENT,
            # "גם לכאלה שהתשלום שלהם לא עבר" — a sign-up not paid yet is registered.
            'pending': ON_REGISTER_STUDENT,
            'inactive': ON_REGISTER_OTHER,
            'trial_signed': ON_REGISTER_OTHER,
            'trial_completed': ON_REGISTER_OTHER,
        }
        for status, kind in expected.items():
            self.assertEqual(register_kind(self.enroll(f'ילד {status}', child_status=status), on), kind, status)


class CancelledAndStillComingTests(HeadcountTestBase):
    """
    "ילד שביטל ועדיין אמור להגיע לסוף החודש — גם הוא רשום. אלא אם כן בוצע לו
    זיכוי באותו היום על כל החודש, ואז הוא לא אמור להגיע יותר."

    Cancelling closes the place at once (drop_course_unit). The register used to
    read only open places, so the child left the instructor's list the same day.
    """

    def setUp(self):
        super().setUp()
        self.wednesday = date.today() + timedelta(days=(2 - date.today().weekday()) % 7 or 7)
        self.enroll('נשאר')
        self.leaver = self.enroll('ביטל')
        self.pay(self.leaver)
        self.cancel(self.leaver)

    def pay(self, enrollment, *, status='completed', lesson=None, bundle=None):
        from datetime import datetime, time as clock

        from django.utils import timezone

        from apps.customers.models import Payment

        child = enrollment.child
        return Payment.objects.create(
            child=child, family=child.family, branch=self.branch, lesson=lesson or enrollment.lesson, bundle=bundle,
            payment_type='recurring_subscription', status=status,
            base_amount=Decimal('235.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('235.00'),
            # The month of the lesson looked at, so the test holds on the last days of a month too.
            payment_date=timezone.make_aware(datetime.combine(self.wednesday.replace(day=1), clock(9, 0))),
        )

    def cancel(self, enrollment, on=None):
        enrollment.status = 'inactive'
        enrollment.end_date = on or self.wednesday.replace(day=1)
        enrollment.save(update_fields=['status', 'end_date', 'updated_at'])

    def names(self, on=None):
        on = on or self.wednesday
        res = self.client.get(f'/api/v1/scheduling/lessons/{self.lesson.id}/', {'date': on.isoformat()})
        self.assertEqual(res.status_code, 200, res.data)
        return {entry['child_name'].split(' כהן')[0]: entry for entry in res.data['enrollments']}

    def registered(self, on=None):
        on = on or self.wednesday
        res = self.client.get('/api/v1/scheduling/lessons/', {'start_date': on.isoformat(), 'end_date': on.isoformat()})
        self.assertEqual(res.status_code, 200, res.data)
        return next(item for item in res.data if item['id'] == str(self.lesson.id))['active_student_count']

    def test_they_stay_on_the_register_and_in_the_count_for_the_month_they_paid(self):
        row = self.names()['ביטל']
        self.assertEqual(row['register_kind'], ON_REGISTER_STUDENT)
        self.assertTrue(row['leaving'])
        self.assertFalse(self.names()['נשאר']['leaving'])
        self.assertEqual(self.registered(), 2)

    def test_a_refund_of_that_month_takes_them_off(self):
        from apps.customers.models import Payment

        Payment.objects.filter(child=self.leaver.child).update(status='refunded')
        self.assertNotIn('ביטל', self.names())
        self.assertEqual(self.registered(), 1)

    def test_the_month_after_they_are_gone(self):
        # Four weeks on is the next month whenever the Wednesday looked at is past the 3rd;
        # the place was closed on the 1st of the month looked at.
        later = self.wednesday + timedelta(days=35)
        self.assertNotEqual((later.year, later.month), (self.wednesday.year, self.wednesday.month))
        self.assertNotIn('ביטל', self.names(later))
        self.assertEqual(self.registered(later), 1)

    def test_a_child_who_never_paid_for_the_month_leaves_on_the_day(self):
        unpaid = self.enroll('ביטל בלי תשלום')
        self.cancel(unpaid)
        self.assertNotIn('ביטל בלי תשלום', self.names())

    def test_a_place_closed_by_a_move_is_not_a_cancellation(self):
        # A move takes the charges along to the new lesson (change_course.replace_unit),
        # so nothing paid stands behind the place that was left.
        from apps.customers.models import Payment

        elsewhere = Lesson.objects.create(
            course=self.course, instructor=self.instructor, day_of_week=5,
            start_time=time(17, 30), end_time=time(18, 15), is_recurring=True, room=self.room,
        )
        Payment.objects.filter(child=self.leaver.child).update(lesson=elsewhere)
        self.assertNotIn('ביטל', self.names())
        self.assertEqual(self.registered(), 1)

    def test_a_track_paid_on_one_of_its_days_covers_the_other_day_too(self):
        from apps.courses.models import LessonBundle
        from apps.customers.models import Payment

        other_day = Lesson.objects.create(
            course=self.course, instructor=self.instructor, day_of_week=5,
            start_time=time(17, 30), end_time=time(18, 15), is_recurring=True, room=self.room,
        )
        bundle = LessonBundle.objects.create(course=self.course, combined_price=Decimal('335.00'))
        bundle.lessons.set([self.lesson, other_day])
        # The charge sits on the other day of the track; this lesson is covered through the track.
        Payment.objects.filter(child=self.leaver.child).update(lesson=other_day, bundle=bundle)
        self.assertIn('ביטל', self.names())

    def test_a_trial_that_was_retired_is_not_a_cancelled_place(self):
        trial = self.enroll('היה בניסיון', child_status='trial_completed', trial_on=self.wednesday.replace(day=1))
        trial.status = 'inactive'
        trial.end_date = self.wednesday.replace(day=1)
        trial.trial_outcome = 'attended'
        trial.save()
        self.pay(trial)
        self.assertNotIn('היה בניסיון', self.names())

    def test_cancelling_frees_the_seat_all_the_same(self):
        res = self.client.get(
            '/api/v1/scheduling/lessons/',
            {'start_date': self.wednesday.isoformat(), 'end_date': self.wednesday.isoformat()},
        )
        row = next(item for item in res.data if item['id'] == str(self.lesson.id))
        self.assertEqual(row['enrollment_count'], 1)
