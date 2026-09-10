"""
External students — the municipality children on our external-branch registers.

The whole feature rests on one guarantee: they are information and nothing
else. They appear on a register, they can be marked, they can be counted and
messaged — and they never reach a payment, a discount, widget capacity, a
standing order or the customers list.

Both halves are asserted here, because both are one edit away from being lost
quietly. The negative tests are the important ones; if a future change makes
external students visible to the money, they are what will say so.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Payment, RecurringPayment
from apps.enrollments.enrollment_counts import (
    count_capacity_enrollments,
    count_distinct_paying_children,
    count_paying_enrollments,
)
from apps.enrollments.models import ChildAbsence, LessonEnrollment
from apps.external_students.models import ExternalStudent, ExternalStudentAttendance
from apps.instructors.models import Instructor

User = get_user_model()

OCC = date(2026, 9, 7)  # a Monday — day_of_week=1 for the lessons below
STUDENTS_URL = '/api/v1/external-students/students/'


def make_user(username, role, **extra):
    user = User.objects.create_user(username=username, password='pw-for-tests', **extra)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return user


class ExternalStudentTestBase(APITestCase):
    def setUp(self):
        self.city = City.objects.create(name='עיר בדיקה')
        self.external_branch = Branch.objects.create(
            name='סניף עירייה', city=self.city, is_external=True,
        )
        self.own_branch = Branch.objects.create(name='סניף שלנו', city=self.city)
        self.ctype = CourseType.objects.create(name='סוג בדיקה')

        self.manager = make_user('manager@ext.test', UserProfile.ROLE_MANAGER, email='manager@ext.test')
        self.worker = make_user('teacher@ext.test', UserProfile.ROLE_WORKER, email='teacher@ext.test')
        self.instructor = Instructor.objects.create(
            first_name='מורה', last_name='בדיקה',
            email='teacher@ext.test', primary_branch=self.external_branch,
        )

        self.lesson = self._lesson('קפוארה עירייה', self.external_branch)
        self.other_lesson = self._lesson('קפוארה עירייה ב', self.external_branch)
        self.own_lesson = self._lesson('קפוארה שלנו', self.own_branch)
        self.auth(self.manager)

    def _lesson(self, name, branch):
        course = Course.objects.create(
            name=name, branch=branch, course_type=self.ctype,
            price=Decimal('235.00'), capacity=20,
        )
        return Lesson.objects.create(
            course=course, instructor=self.instructor,
            day_of_week=1, start_time='16:00', end_time='17:00', is_recurring=True,
        )

    def auth(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def add(self, first='נועם', last='כהן', lesson=None, phone='', **extra):
        return self.client.post(
            STUDENTS_URL,
            {
                'lesson': str((lesson or self.lesson).id),
                'first_name': first, 'last_name': last, 'phone': phone, **extra,
            },
            format='json',
        )

    def student(self, first='נועם', last='כהן', lesson=None, phone=''):
        return ExternalStudent.objects.create(
            lesson=lesson or self.lesson, first_name=first, last_name=last, phone=phone,
        )

    def real_child(self, first='רותם', last='אמיתי', lesson=None):
        """A registered, paying child — everything an external student is not."""
        family = Family.objects.create(name=last, phone='0521111111', branch=self.own_branch)
        child = Child.objects.create(
            family=family, first_name=first, last_name=last,
            birth_date=date(2015, 5, 5), gender='female', status='active',
        )
        enrollment = LessonEnrollment.objects.create(
            lesson=lesson or self.lesson, child=child, status='active', start_date=OCC,
        )
        return enrollment, child, family

    def lesson_detail(self, lesson=None, occ=OCC):
        return self.client.get(
            f'/api/v1/scheduling/lessons/{(lesson or self.lesson).id}/?date={occ}'
        )

    def mark(self, marks, lesson=None, occ=OCC):
        return self.client.post(
            f'/api/v1/scheduling/lessons/{(lesson or self.lesson).id}/mark_attendance/',
            {'date': str(occ), 'attendance': marks},
            format='json',
        )


class CrudTests(ExternalStudentTestBase):
    def test_create_on_an_external_branch(self):
        res = self.add()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        row = ExternalStudent.objects.get(id=res.data['id'])
        self.assertTrue(row.is_active)
        self.assertEqual(row.normalized_name, 'נועם כהן')
        self.assertEqual(row.source, ExternalStudent.SOURCE_MANUAL)
        self.assertEqual(row.created_by, self.manager)

    def test_create_on_a_normal_branch_is_refused(self):
        res = self.add(lesson=self.own_lesson)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('סניף חיצוני', str(res.data))
        self.assertEqual(ExternalStudent.objects.count(), 0)

    def test_the_model_refuses_a_normal_branch_too(self):
        """The shell and any future importer go through save(), not the serializer."""
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            ExternalStudent.objects.create(
                lesson=self.own_lesson, first_name='נועם', last_name='כהן',
            )

    def test_duplicate_active_name_on_the_same_lesson_is_refused(self):
        self.add()
        res = self.add()
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(ExternalStudent.objects.count(), 1)

    def test_the_database_enforces_the_duplicate_rule_as_well(self):
        self.student()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExternalStudent.objects.bulk_create([
                    ExternalStudent(
                        lesson=self.lesson, first_name='נועם', last_name='כהן',
                        normalized_name='נועם כהן',
                    )
                ])

    def test_the_same_name_on_another_lesson_is_allowed(self):
        self.add()
        res = self.add(lesson=self.other_lesson)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    def test_a_student_cannot_be_moved_between_lessons(self):
        row = self.student()
        res = self.client.patch(
            f'{STUDENTS_URL}{row.id}/', {'lesson': str(self.other_lesson.id)}, format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        row.refresh_from_db()
        self.assertEqual(row.lesson_id, self.lesson.id)

    def test_bulk_enters_a_whole_paper_list_at_once(self):
        res = self.client.post(
            f'{STUDENTS_URL}bulk/',
            {
                'lesson': str(self.lesson.id),
                'students': [
                    {'first_name': 'נועם', 'last_name': 'כהן', 'phone': '0501111111'},
                    {'first_name': 'שירה', 'last_name': 'לוי', 'phone': '0502222222'},
                ],
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data['created'], 2)
        self.assertEqual(ExternalStudent.objects.count(), 2)

    def test_a_bad_row_rolls_the_whole_list_back(self):
        res = self.client.post(
            f'{STUDENTS_URL}bulk/',
            {
                'lesson': str(self.lesson.id),
                'students': [
                    {'first_name': 'נועם', 'last_name': 'כהן'},
                    {'first_name': '', 'last_name': ''},
                ],
            },
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(ExternalStudent.objects.count(), 0)

    def test_removing_a_student_with_history_keeps_the_history(self):
        row = self.student()
        ExternalStudentAttendance.objects.create(
            student=row, occurrence_date=OCC, status='present',
        )
        res = self.client.delete(f'{STUDENTS_URL}{row.id}/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.data['deleted'])
        row.refresh_from_db()
        self.assertFalse(row.is_active)
        self.assertEqual(row.end_date, date.today())
        self.assertEqual(ExternalStudentAttendance.objects.count(), 1)

    def test_removing_a_student_with_no_history_deletes_the_row(self):
        row = self.student()
        res = self.client.delete(f'{STUDENTS_URL}{row.id}/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data['deleted'])
        self.assertEqual(ExternalStudent.objects.count(), 0)

    def test_an_inactive_row_does_not_block_re_adding_the_same_name(self):
        row = self.student()
        row.is_active = False
        row.save(update_fields=['is_active'])
        self.assertEqual(self.add().status_code, status.HTTP_201_CREATED)


class PermissionTests(ExternalStudentTestBase):
    def test_a_worker_cannot_read_or_write(self):
        self.student()
        self.auth(self.worker)
        self.assertEqual(self.client.get(STUDENTS_URL).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.add(first='אחר').status_code, status.HTTP_403_FORBIDDEN)

    def test_a_partner_reads_but_cannot_write(self):
        row = self.student()
        partner = make_user('partner@ext.test', UserProfile.ROLE_PARTNER, email='partner@ext.test')
        partner.profile.assigned_branches.add(self.external_branch)
        self.auth(partner)

        listing = self.client.get(STUDENTS_URL)
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual([r['id'] for r in listing.data], [str(row.id)])

        self.assertEqual(self.add(first='אחר').status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            self.client.delete(f'{STUDENTS_URL}{row.id}/').status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_a_partner_sees_nothing_from_a_branch_they_do_not_hold(self):
        self.student()
        other = Branch.objects.create(name='סניף אחר', city=self.city, is_external=True)
        partner = make_user('partner2@ext.test', UserProfile.ROLE_PARTNER, email='partner2@ext.test')
        partner.profile.assigned_branches.add(other)
        self.auth(partner)
        self.assertEqual(self.client.get(STUDENTS_URL).data, [])


class LifecycleGuardTests(ExternalStudentTestBase):
    def test_the_external_flag_cannot_be_turned_off_while_students_are_on_it(self):
        self.student()
        res = self.client.patch(
            f'/api/v1/core/branches/{self.external_branch.id}/',
            {'is_external': False}, format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.external_branch.refresh_from_db()
        self.assertTrue(self.external_branch.is_external)

    def test_the_flag_can_be_turned_off_once_they_are_gone(self):
        row = self.student()
        row.delete()
        res = self.client.patch(
            f'/api/v1/core/branches/{self.external_branch.id}/',
            {'is_external': False}, format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_a_lesson_holding_external_students_cannot_be_deleted(self):
        self.student()
        res = self.client.delete(f'/api/v1/courses/lessons/{self.lesson.id}/')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(Lesson.objects.filter(id=self.lesson.id).exists())

    def test_an_inactive_student_still_blocks_deleting_the_lesson(self):
        """Their attendance history is exactly what this feature exists to produce."""
        row = self.student()
        ExternalStudentAttendance.objects.create(student=row, occurrence_date=OCC, status='present')
        row.is_active = False
        row.save(update_fields=['is_active'])

        res = self.client.delete(f'/api/v1/courses/lessons/{self.lesson.id}/')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(ExternalStudentAttendance.objects.count(), 1)


class RosterAndAttendanceTests(ExternalStudentTestBase):
    def test_the_register_shows_them_with_an_identity_and_a_phone(self):
        row = self.student(phone='050-123-4567')
        enrollment, child, _ = self.real_child()

        res = self.lesson_detail()
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        by_kind = {r['attendee_kind']: r for r in res.data['enrollments']}

        external = by_kind['external']
        self.assertEqual(external['attendee_id'], str(row.id))
        self.assertIsNone(external['child_id'])
        self.assertEqual(external['child_status'], 'external')
        self.assertEqual(external['child_phone'], '050-123-4567')
        self.assertFalse(external['is_trial'])

        real = by_kind['child']
        self.assertEqual(real['attendee_id'], str(child.id))
        self.assertEqual(real['child_id'], str(child.id))

    def test_a_student_outside_their_window_is_not_on_that_register(self):
        row = self.student()
        row.end_date = OCC - timedelta(days=1)
        row.save(update_fields=['end_date'])
        res = self.lesson_detail()
        self.assertEqual(res.data['enrollments'], [])

    def test_marking_an_external_student_writes_only_to_their_own_table(self):
        row = self.student()
        self.auth(self.worker)
        res = self.mark([
            {'attendee_id': str(row.id), 'attendee_kind': 'external', 'status': 'present'}
        ])
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        record = ExternalStudentAttendance.objects.get(student=row)
        self.assertEqual(record.status, 'present')
        self.assertEqual(record.occurrence_date, OCC)
        self.assertEqual(record.marked_by, self.worker)

    def test_marking_twice_updates_the_one_row(self):
        row = self.student()
        self.mark([{'attendee_id': str(row.id), 'attendee_kind': 'external', 'status': 'present'}])
        self.mark([{'attendee_id': str(row.id), 'attendee_kind': 'external', 'status': 'absent'}])
        self.assertEqual(ExternalStudentAttendance.objects.filter(student=row).count(), 1)
        self.assertEqual(ExternalStudentAttendance.objects.get(student=row).status, 'absent')

    def test_a_student_from_another_lesson_is_refused_and_writes_nothing(self):
        elsewhere = self.student(lesson=self.other_lesson)
        res = self.mark([
            {'attendee_id': str(elsewhere.id), 'attendee_kind': 'external', 'status': 'present'}
        ])
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.data['results'][0]['success'])
        self.assertEqual(ExternalStudentAttendance.objects.count(), 0)

    def test_an_external_id_sent_as_a_child_is_an_error_not_a_500(self):
        row = self.student()
        res = self.mark([
            {'attendee_id': str(row.id), 'attendee_kind': 'child', 'status': 'present'}
        ])
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.data['results'][0]['success'])

    def test_the_old_child_id_payload_still_marks_a_real_child(self):
        """The two repositories deploy separately; the old client must keep working."""
        from apps.enrollments.models import LessonAttendance

        _, child, _ = self.real_child()
        res = self.mark([{'child_id': str(child.id), 'status': 'present'}])
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(
            LessonAttendance.objects.get(lesson=self.lesson, child=child).status, 'present',
        )

    def test_the_register_is_incomplete_until_they_are_marked(self):
        row = self.student()

        def week():
            res = self.client.get(
                f'/api/v1/scheduling/lessons/?start_date={OCC}&end_date={OCC}'
            )
            return next(r for r in res.data if r['id'] == str(self.lesson.id))

        before = week()
        self.assertFalse(before['attendance_complete'])
        self.assertEqual(before['external_student_count'], 1)
        self.assertEqual(before['student_count'], 1)
        # The paying count is the one that shares its meaning with widget capacity.
        self.assertEqual(before['enrollment_count'], 0)

        self.mark([{'attendee_id': str(row.id), 'attendee_kind': 'external', 'status': 'present'}])
        self.assertTrue(week()['attendance_complete'])

    def test_missing_registers_lists_the_lesson_until_they_are_marked(self):
        from apps.enrollments.register_reminders import missing_registers

        row = self.student()
        self.assertEqual([m.lesson.id for m in missing_registers(OCC)], [self.lesson.id])

        self.mark([{'attendee_id': str(row.id), 'attendee_kind': 'external', 'status': 'present'}])
        self.assertEqual(missing_registers(OCC), [])

    def test_a_marked_child_does_not_satisfy_an_unmarked_external_student(self):
        """Two uuids from two tables must never stand in for each other."""
        from apps.enrollments.register_reminders import missing_registers

        self.student()
        _, child, _ = self.real_child()
        self.mark([{'child_id': str(child.id), 'status': 'present'}])
        self.assertEqual([m.lesson.id for m in missing_registers(OCC)], [self.lesson.id])


class NoLeakIntoMoneyTests(ExternalStudentTestBase):
    """The tests that matter. Every one of these is a guarantee to the owner."""

    def _add_twenty(self):
        for i in range(20):
            ExternalStudent.objects.create(
                lesson=self.lesson, first_name=f'תלמיד{i}', last_name='עירייה',
                phone=f'05011111{i:02d}',
            )

    def test_paying_and_capacity_counts_do_not_move(self):
        self.real_child()
        before = (
            count_paying_enrollments(lesson=self.lesson),
            count_distinct_paying_children(course=self.lesson.course),
            count_capacity_enrollments(lesson=self.lesson),
        )
        self._add_twenty()
        after = (
            count_paying_enrollments(lesson=self.lesson),
            count_distinct_paying_children(course=self.lesson.course),
            count_capacity_enrollments(lesson=self.lesson),
        )
        self.assertEqual(before, after)

    def test_widget_capacity_does_not_move(self):
        from apps.customers.widget_views import _batch_paying_enrollment_counts

        self.real_child()
        before = _batch_paying_enrollment_counts([self.lesson.id])
        self._add_twenty()
        self.assertEqual(before, _batch_paying_enrollment_counts([self.lesson.id]))

    def test_no_payment_or_standing_order_is_ever_created(self):
        self._add_twenty()
        row = ExternalStudent.objects.first()
        self.mark([{'attendee_id': str(row.id), 'attendee_kind': 'external', 'status': 'present'}])
        self.client.delete(f'{STUDENTS_URL}{row.id}/')
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(RecurringPayment.objects.count(), 0)

    def test_they_have_no_family_so_the_sibling_discount_cannot_see_them(self):
        """
        The second-child discount reads LessonEnrollment by child__family_id.

        An external student has no family and is not a Child, so the query it
        runs cannot reach one. Asserting the absence of the attribute is the
        honest way to pin that: it is what makes the discount safe.
        """
        row = self.student()
        self.assertFalse(hasattr(row, 'family'))
        self.assertFalse(hasattr(row, 'family_id'))

    def test_marking_absent_creates_no_absence_and_sends_no_whatsapp(self):
        row = self.student(phone='0501234567')
        with patch(
            'apps.enrollments.attendance_whatsapp.maybe_send_didnt_arrive_whatsapp'
        ) as whatsapp:
            self.mark([
                {'attendee_id': str(row.id), 'attendee_kind': 'external', 'status': 'absent'}
            ])
        whatsapp.assert_not_called()
        self.assertEqual(ChildAbsence.objects.count(), 0)

    def test_they_are_not_in_the_customers_list(self):
        self._add_twenty()
        res = self.client.get('/api/v1/customers/children/')
        rows = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        self.assertEqual(rows, [])
        self.assertEqual(Child.objects.count(), 0)

    def test_the_salary_calculation_is_byte_identical(self):
        """
        The counter exists; nothing calls it yet.

        The instructor is put on tiers first, deliberately: on fixed pay the
        headcount is ignored and this test would pass no matter what the count
        did. With tiers, twenty extra heads would jump the bracket, so the
        assertion has something real to hold.

        This is the test to change deliberately on the day the owner wires
        external students into the tier — not one to quietly let drift.
        """
        from apps.instructors.models import InstructorSalaryTier
        from apps.instructors.utils import calculate_instructor_salary_for_month

        self.instructor.salary_model_type = 'tiered_by_students'
        self.instructor.save(update_fields=['salary_model_type'])
        InstructorSalaryTier.objects.create(
            instructor=self.instructor, min_students=1, max_students=10,
            salary_per_lesson=Decimal('200.00'),
        )
        InstructorSalaryTier.objects.create(
            instructor=self.instructor, min_students=11, max_students=None,
            salary_per_lesson=Decimal('400.00'),
        )

        self.real_child()
        before = calculate_instructor_salary_for_month(self.instructor, '2026-09')
        self._add_twenty()
        after = calculate_instructor_salary_for_month(self.instructor, '2026-09')
        self.assertEqual(before, after)
        # And prove the tier really is sensitive to a headcount, so a future
        # reader knows the equality above was worth asserting.
        for _ in range(15):
            self.real_child(first=f'ילד{_}', last='משלם')
        self.assertNotEqual(before, calculate_instructor_salary_for_month(self.instructor, '2026-09'))

    def test_the_monthly_snapshots_are_byte_identical(self):
        from apps.core.models import BranchMonthlySnapshot, LessonMonthlySnapshot
        from apps.instructors.utils import generate_monthly_snapshots

        self.real_child()
        generate_monthly_snapshots('2026-09')
        before_lesson = list(
            LessonMonthlySnapshot.objects.filter(month='2026-09')
            .order_by('lesson_id').values_list('lesson_id', 'enrolled_students', 'revenue')
        )
        before_branch = list(
            BranchMonthlySnapshot.objects.filter(month='2026-09')
            .order_by('branch_id').values_list('branch_id', 'total_students')
        )

        self._add_twenty()
        generate_monthly_snapshots('2026-09')
        self.assertEqual(before_lesson, list(
            LessonMonthlySnapshot.objects.filter(month='2026-09')
            .order_by('lesson_id').values_list('lesson_id', 'enrolled_students', 'revenue')
        ))
        self.assertEqual(before_branch, list(
            BranchMonthlySnapshot.objects.filter(month='2026-09')
            .order_by('branch_id').values_list('branch_id', 'total_students')
        ))

    def test_the_course_page_keeps_the_two_numbers_apart(self):
        self.real_child()
        self._add_twenty()
        res = self.client.get(f'/api/v1/courses/courses/?branch_id={self.external_branch.id}')
        row = next(r for r in res.data if r['id'] == str(self.lesson.course_id))
        self.assertEqual(row['enrolled_students_count'], 1)
        self.assertEqual(row['external_students_count'], 20)


class BroadcastTests(ExternalStudentTestBase):
    def _send(self, ids, dry_run=True, **extra):
        return self.client.post(
            f'{STUDENTS_URL}broadcast/',
            {
                'student_ids': [str(i) for i in ids],
                'automation_id': 'flow-ns',
                'dry_run': dry_run,
                **extra,
            },
            format='json',
        )

    def test_a_preview_sends_nothing(self):
        row = self.student(phone='0501234567')
        with patch('apps.core.manychat_service.ManyChatService.send_automation_to_contact') as send:
            res = self._send([row.id])
        send.assert_not_called()
        self.assertEqual(res.data['preview_count'], 1)
        self.assertEqual(res.data['sent'], 0)

    def test_a_real_send_goes_out_as_a_flow(self):
        row = self.student(phone='0501234567')
        with patch(
            'apps.core.manychat_service.ManyChatService.send_automation_to_contact',
            return_value={'sent': True, 'method': 'flow'},
        ) as send:
            res = self._send([row.id], dry_run=False)
        self.assertEqual(res.data['sent'], 1)
        self.assertEqual(send.call_args.kwargs['automation_type'], 'flow')
        self.assertEqual(send.call_args.kwargs['phone'], '0501234567')

    def test_a_student_without_a_phone_is_skipped_not_failed(self):
        row = self.student(phone='')
        with patch('apps.core.manychat_service.ManyChatService.send_automation_to_contact') as send:
            res = self._send([row.id], dry_run=False)
        send.assert_not_called()
        self.assertEqual(res.data['skipped'], 1)
        self.assertEqual(res.data['results'][0]['reason'], 'no_phone')

    def test_one_phone_gets_one_message(self):
        a = self.student(first='נועם', phone='050-123-4567')
        b = self.student(first='שירה', phone='0501234567')
        with patch(
            'apps.core.manychat_service.ManyChatService.send_automation_to_contact',
            return_value={'sent': True},
        ) as send:
            res = self._send([a.id, b.id], dry_run=False)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(res.data['sent'], 1)
        self.assertEqual(res.data['skipped'], 1)

    def test_a_worker_cannot_broadcast(self):
        row = self.student(phone='0501234567')
        self.auth(self.worker)
        self.assertEqual(self._send([row.id]).status_code, status.HTTP_403_FORBIDDEN)
