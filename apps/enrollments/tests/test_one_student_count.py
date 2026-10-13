"""
One number of students for a lesson, on every screen.

Owner, 13.10.2026: "כמות התלמידים שמופיעים ביומן במערכת של ה-admin לא תואמים
למשתמשים של המורים… זה חמור וחייב שתוודא שכל המערכת מסונכרנת ומה נכון."

Four figures were on show for one lesson. The office's calendar drew the seats
taken (no trial statuses, no municipality children). The instructor's day drew
everyone on the register who was not a trial that day. The lesson window drew
the length of the register. Pay and the catalogue counted by the owner's own
rule of 24.9.2026 — a student is a child who is פעיל or בעיית תשלום.

That rule is the one. `register_kind` says what each row on a register is, the
schedule counts by it, and the register sends it with every row.
"""
from datetime import date, timedelta

from rest_framework.authtoken.models import Token

from apps.core.models import UserProfile
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

    def test_students_are_the_children_who_are_active_or_on_a_payment_problem(self):
        row = self.calendar_row()
        self.assertEqual(row['active_student_count'], 4)
        self.assertEqual(row['trial_student_count'], 1)
        # Not paid yet, left without the place being closed, an old trial with no date.
        self.assertEqual(row['other_student_count'], 3)
        self.assertEqual(row['student_count'], 8)

    def test_the_register_carries_the_same_split_row_by_row(self):
        row = self.calendar_row()
        kinds = self.kinds(self.register())
        self.assertEqual(kinds[ON_REGISTER_STUDENT], row['active_student_count'])
        self.assertEqual(kinds[ON_REGISTER_TRIAL], row['trial_student_count'])
        self.assertEqual(kinds[ON_REGISTER_OTHER], row['other_student_count'])
        self.assertEqual(sum(kinds.values()), row['student_count'])

    def test_nobody_is_taken_off_the_register(self):
        names = {entry['child_name'].split(' כהן')[0] for entry in self.register()}
        self.assertIn('לא שילם עדיין', names)
        self.assertIn('עזב ולא נסגר', names)
        self.assertIn('ניסיון ישן בלי תאריך', names)
        self.assertNotIn('ניסיון בשבוע הבא', names)

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
        self.assertEqual(row['active_student_count'], 5)
        self.assertEqual(self.kinds(self.register())[ON_REGISTER_STUDENT], 5)

    def test_the_municipalitys_children_are_students_of_the_lesson(self):
        self.branch.is_external = True
        self.branch.save(update_fields=['is_external'])
        ExternalStudent.objects.create(lesson=self.lesson, first_name='ילד', last_name='עירייה', is_active=True)
        row = self.calendar_row()
        self.assertEqual(row['active_student_count'], 5)
        self.assertEqual(row['external_student_count'], 1)
        self.assertEqual(self.kinds(self.register())[ON_REGISTER_STUDENT], 5)

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
            'pending': ON_REGISTER_OTHER,
            'inactive': ON_REGISTER_OTHER,
            'trial_signed': ON_REGISTER_OTHER,
            'trial_completed': ON_REGISTER_OTHER,
        }
        for status, kind in expected.items():
            self.assertEqual(register_kind(self.enroll(f'ילד {status}', child_status=status), on), kind, status)
