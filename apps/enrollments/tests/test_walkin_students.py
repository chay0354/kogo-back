"""
Walk-ins added by an instructor from the attendance screen.

The whole feature rests on one guarantee: a walk-in is on the register and
nowhere else. They must never reach capacity, pay, billing, an invoice or a
WhatsApp message, and a registered child must never be removable from this
screen. Both halves are asserted here, because both are one edit away from
being lost quietly.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, LinkedUserAccess, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family, Payment
from apps.enrollments.enrollment_counts import (
    count_capacity_enrollments,
    count_paying_enrollments,
)
from apps.enrollments.models import LessonAttendance, LessonEnrollment
from apps.instructors.models import Instructor

User = get_user_model()

OCC = date(2026, 9, 7)  # a Monday, which is the day_of_week=1 the lessons below meet on


def make_worker(username, **extra):
    user = User.objects.create_user(username=username, password='pw-for-tests', **extra)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = UserProfile.ROLE_WORKER
    profile.save(update_fields=['role'])
    return user


class WalkInTestBase(APITestCase):
    def setUp(self):
        self.city = City.objects.create(name='עיר בדיקה')
        self.branch = Branch.objects.create(name='סניף בדיקה', city=self.city)
        self.ctype = CourseType.objects.create(name='סוג בדיקה')

        self.user = make_worker('teacher@walkin.test', email='teacher@walkin.test')
        self.instructor = Instructor.objects.create(
            first_name='מורה', last_name='בדיקה',
            email='teacher@walkin.test', primary_branch=self.branch,
        )
        self.lesson = self._lesson('חוג בדיקה', self.instructor)
        self.auth(self.user)

    def _lesson(self, name, instructor):
        course = Course.objects.create(
            name=name, branch=self.branch, course_type=self.ctype,
            price=Decimal('200.00'), capacity=12,
        )
        return Lesson.objects.create(
            course=course, instructor=instructor,
            day_of_week=1, start_time='16:00', end_time='17:00', is_recurring=True,
        )

    def auth(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def add_walkin(self, first='נועם', last='כהן', phone='', lesson=None, as_user=None):
        lesson = lesson or self.lesson
        suffix = f'?as_user={as_user.id}' if as_user else ''
        return self.client.post(
            f'/api/v1/scheduling/lessons/{lesson.id}/add-walkin/{suffix}',
            {'date': str(OCC), 'first_name': first, 'last_name': last, 'phone': phone},
            format='json',
        )

    def remove_walkin(self, enrollment_id, lesson=None, as_user=None):
        lesson = lesson or self.lesson
        suffix = f'?as_user={as_user.id}' if as_user else ''
        return self.client.delete(
            f'/api/v1/scheduling/lessons/{lesson.id}/walkin/{enrollment_id}/{suffix}'
        )

    def real_child(self, first='רותם', last='אמיתי'):
        """A registered child on the lesson — everything a walk-in is not."""
        family = Family.objects.create(name=last, phone='0521111111')
        child = Child.objects.create(
            family=family, first_name=first, last_name=last,
            birth_date=date(2015, 5, 5), gender='female', status='active',
        )
        enrollment = LessonEnrollment.objects.create(
            lesson=self.lesson, child=child, status='active', start_date=OCC,
        )
        return enrollment, child, family


class WalkInIsMarkedPresentTests(WalkInTestBase):
    def test_a_walkin_is_present_the_moment_it_is_created(self):
        res = self.add_walkin()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        enrollment = LessonEnrollment.objects.get(id=res.data['id'])
        record = LessonAttendance.objects.get(
            lesson=self.lesson, child=enrollment.child, occurrence_date=OCC,
        )
        self.assertEqual(record.status, 'present')

    def test_the_mark_travels_in_the_answer_so_no_second_request_is_needed(self):
        res = self.add_walkin()
        self.assertEqual(res.data['attendance_status'], 'present')

    def test_the_mark_lands_on_the_occurrence_asked_for_not_today(self):
        res = self.add_walkin()
        enrollment = LessonEnrollment.objects.get(id=res.data['id'])
        dates = list(
            LessonAttendance.objects
            .filter(lesson=self.lesson, child=enrollment.child)
            .values_list('occurrence_date', flat=True)
        )
        self.assertEqual(dates, [OCC])

    def test_a_repeat_tap_marks_present_again_without_a_second_row(self):
        first = self.add_walkin()
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        LessonAttendance.objects.filter(lesson=self.lesson).update(status='absent')

        again = self.add_walkin()
        self.assertEqual(again.status_code, status.HTTP_200_OK)
        self.assertEqual(again.data['id'], first.data['id'])
        self.assertEqual(LessonEnrollment.objects.filter(lesson=self.lesson).count(), 1)
        self.assertEqual(LessonAttendance.objects.filter(lesson=self.lesson).count(), 1)
        self.assertEqual(LessonAttendance.objects.get().status, 'present')

    def test_the_register_shows_the_walkin_already_ticked(self):
        self.add_walkin()
        res = self.client.get(
            f'/api/v1/scheduling/lessons/{self.lesson.id}/', {'date': str(OCC)},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        marks = {a['child_name']: a['status'] for a in res.data['attendance']}
        self.assertEqual(marks.get('נועם כהן'), 'present')


class WalkInReachesNoMoneyAndNoMessagingTests(WalkInTestBase):
    """
    The guarantee the whole feature rests on. Being marked present must not
    move a walk-in into any count that decides money.
    """

    def test_a_present_walkin_still_counts_for_nothing(self):
        res = self.add_walkin()
        enrollment = LessonEnrollment.objects.get(id=res.data['id'])

        self.assertEqual(enrollment.status, 'inactive')
        self.assertEqual(enrollment.child.status, 'ghost')
        self.assertEqual(count_paying_enrollments(lesson=self.lesson), 0)
        self.assertEqual(
            count_capacity_enrollments(lesson=self.lesson, occurrence_date=OCC), 0
        )

    def test_a_present_walkin_produces_no_payment_and_no_invoice(self):
        res = self.add_walkin()
        child = LessonEnrollment.objects.get(id=res.data['id']).child

        self.assertFalse(Payment.objects.filter(child=child).exists())
        self.assertFalse(child.recurring_payments.exists())
        self.assertFalse(child.invoice_entries.exists())
        self.assertFalse(child.family.payments.exists())
        self.assertFalse(child.family.invoices.exists())

    def test_marking_a_walkin_sends_no_whatsapp(self):
        with patch('apps.core.manychat_service.ManyChatService.notify_registration') as notify:
            self.add_walkin()
            res = self.add_walkin(first='דנה', last='לוי')
            enrollment = LessonEnrollment.objects.get(id=res.data['id'])
            # And not through the marking endpoint either, however it is marked.
            for mark in ('absent', 'present', 'absent'):
                self.client.post(
                    f'/api/v1/scheduling/lessons/{self.lesson.id}/mark_attendance/',
                    {'date': str(OCC),
                     'attendance': [{'child_id': str(enrollment.child_id), 'status': mark}]},
                    format='json',
                )
        notify.assert_not_called()

    def test_a_walkin_does_not_join_the_lesson_capacity_shown_on_the_day(self):
        self.real_child()
        self.add_walkin()
        res = self.client.get(
            '/api/v1/scheduling/lessons/',
            {'start_date': str(OCC), 'end_date': str(OCC)},
        )
        row = next(r for r in res.data if r['id'] == str(self.lesson.id))
        # One registered child, counted once. The day's counters are built from
        # active enrolments only, so a walk-in reaches neither the paying number
        # nor the headcount on the card — it exists on the register and there
        # alone.
        self.assertEqual(row['enrollment_count'], 1)
        self.assertEqual(row['student_count'], 1)
        self.assertEqual(row['active_student_count'], 1)
        self.assertEqual(row['trial_student_count'], 0)


class WalkInRemovalTests(WalkInTestBase):
    def test_removing_a_walkin_takes_the_row_and_its_mark(self):
        added = self.add_walkin()
        enrollment_id = added.data['id']
        child_id = added.data['child_id']

        res = self.remove_walkin(enrollment_id)
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)

        self.assertFalse(LessonEnrollment.objects.filter(id=enrollment_id).exists())
        self.assertFalse(LessonAttendance.objects.filter(child_id=child_id).exists())
        self.assertFalse(Child.objects.filter(id=child_id).exists())

    def test_removing_a_walkin_collects_the_family_it_invented(self):
        added = self.add_walkin()
        child = Child.objects.get(id=added.data['child_id'])
        family_id = child.family_id

        self.remove_walkin(added.data['id'])
        self.assertFalse(Family.objects.filter(id=family_id).exists())

    def test_the_walkin_leaves_the_register(self):
        added = self.add_walkin()
        self.remove_walkin(added.data['id'])

        res = self.client.get(
            f'/api/v1/scheduling/lessons/{self.lesson.id}/', {'date': str(OCC)},
        )
        self.assertEqual(res.data['enrollments'], [])
        self.assertEqual(res.data['attendance'], [])

    def test_a_registered_child_is_refused(self):
        enrollment, child, family = self.real_child()

        res = self.remove_walkin(enrollment.id)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(LessonEnrollment.objects.filter(id=enrollment.id).exists())
        self.assertTrue(Child.objects.filter(id=child.id).exists())
        self.assertTrue(Family.objects.filter(id=family.id).exists())

    def test_an_enrolment_that_was_activated_is_no_longer_a_walkin(self):
        """The office turned it into a registration; this screen loses the right."""
        added = self.add_walkin()
        enrollment = LessonEnrollment.objects.get(id=added.data['id'])
        enrollment.status = 'active'
        enrollment.save(update_fields=['status'])

        res = self.remove_walkin(enrollment.id)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(LessonEnrollment.objects.filter(id=enrollment.id).exists())

    def test_a_child_promoted_out_of_ghost_is_no_longer_a_walkin(self):
        added = self.add_walkin()
        enrollment = LessonEnrollment.objects.get(id=added.data['id'])
        Child.objects.filter(id=enrollment.child_id).update(status='active')

        res = self.remove_walkin(enrollment.id)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Child.objects.filter(id=enrollment.child_id).exists())

    def test_a_walkin_on_another_lesson_is_not_reachable_through_this_one(self):
        other = self._lesson('חוג אחר', self.instructor)
        added = self.add_walkin(lesson=other)

        res = self.remove_walkin(added.data['id'])  # asked for on self.lesson
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(LessonEnrollment.objects.filter(id=added.data['id']).exists())

    def test_an_id_that_is_not_an_id_is_refused_rather_than_crashing(self):
        res = self.remove_walkin('not-a-uuid')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_child_something_else_points_at_is_kept(self):
        """
        Deleting a Child cascades into Payment, RecurringPayment and
        InvoiceChild. Whatever put a payment there, it outranks an undo tap.
        """
        added = self.add_walkin()
        enrollment = LessonEnrollment.objects.get(id=added.data['id'])
        child = enrollment.child
        Payment.objects.create(
            family=child.family, child=child, payment_type='lesson',
            base_amount=Decimal('100.00'), final_amount=Decimal('100.00'),
        )

        res = self.remove_walkin(enrollment.id)
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        # Off the register, as asked...
        self.assertFalse(LessonEnrollment.objects.filter(id=enrollment.id).exists())
        # ...but the record and the money survive.
        self.assertTrue(Child.objects.filter(id=child.id).exists())
        self.assertTrue(Family.objects.filter(id=child.family_id).exists())
        self.assertEqual(Payment.objects.filter(child=child).count(), 1)

    def test_a_family_with_another_child_is_kept(self):
        added = self.add_walkin()
        enrollment = LessonEnrollment.objects.get(id=added.data['id'])
        family = enrollment.child.family
        sibling = Child.objects.create(
            family=family, first_name='אח', last_name='נוסף',
            birth_date=date(2016, 1, 1), gender='male', status='active',
        )

        self.remove_walkin(enrollment.id)
        self.assertTrue(Family.objects.filter(id=family.id).exists())
        self.assertTrue(Child.objects.filter(id=sibling.id).exists())


class WalkInLinkedColleagueTests(APITestCase):
    """
    Covering a colleague reaches exactly the registers the link grants, and the
    new removal is scoped no differently from everything else on the screen.
    """

    def setUp(self):
        self.city = City.objects.create(name='עיר בדיקה')
        self.branch = Branch.objects.create(name='סניף בדיקה', city=self.city)
        self.ctype = CourseType.objects.create(name='סוג בדיקה')

        self.alice = make_worker('alice@walkin.test', email='alice@walkin.test')
        self.bob = make_worker('bob@walkin.test', email='bob@walkin.test')
        self.carol = make_worker('carol@walkin.test', email='carol@walkin.test')

        bob_instructor = Instructor.objects.create(
            first_name='בוב', last_name='מדריך',
            email='bob@walkin.test', primary_branch=self.branch,
        )
        carol_instructor = Instructor.objects.create(
            first_name='קרול', last_name='מדריכה',
            email='carol@walkin.test', primary_branch=self.branch,
        )
        self.bob_lesson = self._lesson('חוג של בוב', bob_instructor)
        self.carol_lesson = self._lesson('חוג של קרול', carol_instructor)

        # Alice may cover Bob. Nobody granted her anything of Carol's.
        LinkedUserAccess.objects.create(owner=self.alice, linked=self.bob)

    def _lesson(self, name, instructor):
        course = Course.objects.create(
            name=name, branch=self.branch, course_type=self.ctype,
            price=Decimal('200.00'), capacity=12,
        )
        return Lesson.objects.create(
            course=course, instructor=instructor,
            day_of_week=1, start_time='16:00', end_time='17:00', is_recurring=True,
        )

    def auth(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def add(self, lesson, as_user):
        return self.client.post(
            f'/api/v1/scheduling/lessons/{lesson.id}/add-walkin/?as_user={as_user.id}',
            {'date': str(OCC), 'first_name': 'נועם', 'last_name': 'כהן'},
            format='json',
        )

    def remove(self, lesson, enrollment_id, as_user):
        return self.client.delete(
            f'/api/v1/scheduling/lessons/{lesson.id}/walkin/{enrollment_id}/'
            f'?as_user={as_user.id}'
        )

    def test_a_linked_colleague_can_add_and_remove_on_a_lesson_they_may_open(self):
        self.auth(self.alice)
        added = self.add(self.bob_lesson, self.bob)
        self.assertEqual(added.status_code, status.HTTP_201_CREATED)

        res = self.remove(self.bob_lesson, added.data['id'], self.bob)
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(LessonEnrollment.objects.filter(id=added.data['id']).exists())

    def test_a_linked_colleague_can_do_neither_on_a_lesson_they_may_not_open(self):
        self.auth(self.alice)
        added = self.add(self.carol_lesson, self.carol)
        self.assertEqual(added.status_code, status.HTTP_403_FORBIDDEN)

        # And no reach at an existing row on that lesson either.
        self.auth(self.carol)
        real = self.client.post(
            f'/api/v1/scheduling/lessons/{self.carol_lesson.id}/add-walkin/',
            {'date': str(OCC), 'first_name': 'דנה', 'last_name': 'לוי'},
            format='json',
        )
        self.assertEqual(real.status_code, status.HTTP_201_CREATED)

        self.auth(self.alice)
        res = self.remove(self.carol_lesson, real.data['id'], self.carol)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(LessonEnrollment.objects.filter(id=real.data['id']).exists())

    def test_naming_an_account_without_a_link_grants_nothing(self):
        self.auth(self.alice)
        self.auth(self.bob)
        added = self.add(self.bob_lesson, self.bob)
        self.assertEqual(added.status_code, status.HTTP_201_CREATED)

        # Carol has no link to Bob, so Bob's id in the query string is just a string.
        self.auth(self.carol)
        res = self.remove(self.bob_lesson, added.data['id'], self.bob)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(LessonEnrollment.objects.filter(id=added.data['id']).exists())


class WalkInDisappearsOnceRegisteredTests(WalkInTestBase):
    """
    The promise the instructor screen now makes in words: a ghost goes away by
    itself once the office registers the child.

    It is resolved when the roster is read, not by a nightly job, so the ghost
    can never outlive the real record — but only against the enrolments of the
    lesson it sits on. That boundary is asserted here too, because it is the one
    that surprises people.
    """

    def roster(self, lesson=None):
        lesson = lesson or self.lesson
        res = self.client.get(f'/api/v1/scheduling/lessons/{lesson.id}/?date={OCC}')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        return res.data['enrollments']

    def ghosts_on(self, lesson=None):
        return [r for r in self.roster(lesson) if r['child_status'] == 'ghost']

    def names_on(self, lesson=None):
        return [r['child_name'] for r in self.roster(lesson)]

    def register(self, *, first, last, phone, trial=False, lesson=None):
        """Register a real child on the lesson — as a trial or as a subscriber."""
        lesson = lesson or self.lesson
        family = Family.objects.create(name=last, phone=phone)
        child = Child.objects.create(
            family=family, first_name=first, last_name=last,
            birth_date=date(2015, 5, 5), gender='male',
            status='trial_signed' if trial else 'active',
        )
        LessonEnrollment.objects.create(
            lesson=lesson, child=child, status='active', start_date=OCC,
            **({'trial_lesson_date': OCC} if trial else {}),
        )
        return child

    def test_a_ghost_shows_until_somebody_registers_the_child(self):
        self.add_walkin(first='יאיר', last='ציון', phone='0544320500')
        self.assertEqual([g['child_name'] for g in self.ghosts_on()], ['יאיר ציון'])

    def test_registering_for_a_trial_takes_the_ghost_off_the_register(self):
        self.add_walkin(first='יאיר', last='ציון', phone='0544320500')
        self.register(first='יאיר', last='ציון', phone='0544320500', trial=True)
        self.assertEqual(self.ghosts_on(), [], 'הרפאים ירד')
        self.assertEqual(self.names_on().count('יאיר ציון'), 1, 'מופיע פעם אחת, כתלמיד ניסיון')

    def test_registering_as_a_subscriber_takes_the_ghost_off_the_register(self):
        self.add_walkin(first='יאיר', last='ציון', phone='0544320500')
        self.register(first='יאיר', last='ציון', phone='0544320500')
        self.assertEqual(self.ghosts_on(), [])
        self.assertEqual(self.names_on().count('יאיר ציון'), 1)

    def test_the_same_phone_is_enough_even_under_another_spelling(self):
        """Parents spell a name two ways; the phone is the harder fact."""
        self.add_walkin(first='יאיר', last='ציון', phone='054-432-0500')
        self.register(first='יאיר', last='ציוני', phone='0544320500')
        self.assertEqual(self.ghosts_on(), [])

    def test_the_same_full_name_is_enough_when_no_phone_was_typed(self):
        self.add_walkin(first='יאיר', last='ציון', phone='')
        self.register(first='יאיר', last='ציון', phone='0559999999')
        self.assertEqual(self.ghosts_on(), [])

    def test_a_different_child_does_not_take_the_ghost_away(self):
        self.add_walkin(first='יאיר', last='ציון', phone='0544320500')
        self.register(first='רותם', last='אחר', phone='0521234567')
        self.assertEqual([g['child_name'] for g in self.ghosts_on()], ['יאיר ציון'])

    def test_registering_on_another_lesson_clears_this_ghost_too(self):
        """
        A ghost stands in for a child the system does not hold. Once the office
        registers that child — any lesson — the stand-in is a second row for one
        person, so it goes wherever it sits.
        """
        other = self._lesson('חוג אחר', self.instructor)
        self.add_walkin(first='יאיר', last='ציון', phone='0544320500')
        self.register(first='יאיר', last='ציון', phone='0544320500', lesson=other)
        self.assertEqual(self.ghosts_on(), [])

    def test_a_trial_on_another_lesson_clears_it_as_well(self):
        other = self._lesson('חוג שלישי', self.instructor)
        self.add_walkin(first='יאיר', last='ציון', phone='0544320500')
        self.register(first='יאיר', last='ציון', phone='0544320500', trial=True, lesson=other)
        self.assertEqual(self.ghosts_on(), [])

    def test_a_child_registered_with_no_lesson_at_all_still_clears_it(self):
        """The office may open the card before choosing a course."""
        self.add_walkin(first='יאיר', last='ציון', phone='0544320500')
        family = Family.objects.create(name='ציון', phone='0544320500')
        Child.objects.create(
            family=family, first_name='יאיר', last_name='ציון',
            birth_date=date(2015, 5, 5), gender='male', status='active',
        )
        self.assertEqual(self.ghosts_on(), [])

    def test_a_namesake_elsewhere_does_not_clear_it(self):
        """
        The asymmetry: a phone identifies a family, a name does not. Two children
        can share a full name across branches, and clearing on that would delete
        a real walk-in from a register somebody is standing in front of.
        """
        other = self._lesson('חוג רביעי', self.instructor)
        self.add_walkin(first='יאיר', last='ציון', phone='0544320500')
        self.register(first='יאיר', last='ציון', phone='0559999999', lesson=other)
        self.assertEqual([g['child_name'] for g in self.ghosts_on()], ['יאיר ציון'])

    def test_another_ghost_elsewhere_is_not_a_registration(self):
        other = self._lesson('חוג חמישי', self.instructor)
        self.add_walkin(first='יאיר', last='ציון', phone='0544320500')
        self.add_walkin(first='יאיר', last='ציון', phone='0544320500', lesson=other)
        self.assertEqual([g['child_name'] for g in self.ghosts_on()], ['יאיר ציון'])



class GhostWithoutAPhoneTests(WalkInTestBase):
    """
    A walk-in typed in with no phone has only a name to go on.

    It clears across lessons only when that name is unmistakable in the branch —
    held by exactly one registered child there. Otherwise it stays and runs out
    with its window: a ghost left up a little longer is a small cost, a real
    child removed from a register is not.
    """

    def ghosts_on(self, lesson=None):
        lesson = lesson or self.lesson
        res = self.client.get(f'/api/v1/scheduling/lessons/{lesson.id}/?date={OCC}')
        return [r for r in res.data['enrollments'] if r['child_status'] == 'ghost']

    def register(self, *, first, last, phone='', lesson=None, branch=None, trial=False):
        lesson = lesson or self._lesson(f'חוג {first}', self.instructor)
        family = Family.objects.create(name=last, phone=phone, branch=branch or self.branch)
        child = Child.objects.create(
            family=family, first_name=first, last_name=last,
            birth_date=date(2015, 5, 5), gender='male',
            status='trial_signed' if trial else 'active',
        )
        LessonEnrollment.objects.create(
            lesson=lesson, child=child, status='active', start_date=OCC,
            **({'trial_lesson_date': OCC} if trial else {}),
        )
        return child

    def test_one_namesake_registered_in_the_branch_clears_it(self):
        self.add_walkin(first='נועם', last='בלי', phone='')
        self.register(first='נועם', last='בלי', phone='0521111111')
        self.assertEqual(self.ghosts_on(), [])

    def test_a_trial_in_the_branch_clears_it_too(self):
        self.add_walkin(first='נועם', last='בלי', phone='')
        self.register(first='נועם', last='בלי', phone='0521111111', trial=True)
        self.assertEqual(self.ghosts_on(), [])

    def test_two_namesakes_in_the_branch_leave_it_standing(self):
        """Ambiguous: which of them is the walk-in? Not ours to guess."""
        self.add_walkin(first='נועם', last='בלי', phone='')
        self.register(first='נועם', last='בלי', phone='0521111111')
        self.register(first='נועם', last='בלי', phone='0522222222')
        self.assertEqual(len(self.ghosts_on()), 1)

    def test_a_namesake_in_another_branch_leaves_it_standing(self):
        other_branch = Branch.objects.create(name='סניף אחר', city=self.city)
        other_course = Course.objects.create(
            name='חוג בסניף אחר', branch=other_branch, course_type=self.ctype,
            price=Decimal('200.00'), capacity=12,
        )
        other_lesson = Lesson.objects.create(
            course=other_course, instructor=self.instructor,
            day_of_week=1, start_time='18:00', end_time='19:00', is_recurring=True,
        )
        self.add_walkin(first='נועם', last='בלי', phone='')
        self.register(first='נועם', last='בלי', phone='0521111111',
                      lesson=other_lesson, branch=other_branch)
        self.assertEqual(len(self.ghosts_on()), 1)

    def test_a_ghost_with_a_phone_is_not_cleared_by_a_namesake_with_another(self):
        """A phone identifies; a namesake on a different number is somebody else."""
        self.add_walkin(first='נועם', last='בלי', phone='0540000000')
        self.register(first='נועם', last='בלי', phone='0521111111')
        self.assertEqual(len(self.ghosts_on()), 1)

    def test_a_ghost_does_not_count_as_the_registered_namesake(self):
        other = self._lesson('חוג שני', self.instructor)
        self.add_walkin(first='נועם', last='בלי', phone='')
        self.add_walkin(first='נועם', last='בלי', phone='', lesson=other)
        self.assertEqual(len(self.ghosts_on()), 1)
