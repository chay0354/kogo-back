"""
A walk-in's life on the register (owner, 14.10.2026).

* Shown on the lesson it came to, and on the next one with "הגיע בשיעור שעבר" —
  the last time, unless the instructor marks it present again.
* The instructor can add its phone afterwards; without one it is no lead, and
  once its last lesson has passed it is removed altogether.
* Five minutes before the lesson ends, the instructor is told which walk-ins
  still have no phone.
* The office sees which lesson it came to and when.
"""
from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.utils import timezone
from rest_framework import status

from apps.customers.models import Child, Family
from apps.enrollments.ghost_students import remove_expired_phoneless_ghosts
from apps.enrollments.models import LessonAttendance, LessonEnrollment, RegisterReminder
from apps.enrollments.register_reminders import send_due_register_reminders
from apps.enrollments.tests.test_walkin_students import OCC, WalkInTestBase
from apps.scheduling.models import LessonCancellation

NEXT = OCC + timedelta(weeks=1)
THIRD = OCC + timedelta(weeks=2)


class WalkInRegisterBase(WalkInTestBase):
    def row(self, day, lesson=None):
        lesson = lesson or self.lesson
        res = self.client.get(f'/api/v1/scheduling/lessons/{lesson.id}/?date={day}')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ghosts = [r for r in res.data['enrollments'] if r['child_status'] == 'ghost']
        return ghosts[0] if ghosts else None

    def mark(self, child_id, day, mark='present'):
        res = self.client.post(
            f'/api/v1/scheduling/lessons/{self.lesson.id}/mark_attendance/',
            {'date': str(day), 'attendance': [{'child_id': child_id, 'status': mark}]},
            format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)

    def update(self, enrollment_id, body):
        return self.client.patch(
            f'/api/v1/scheduling/lessons/{self.lesson.id}/walkin/{enrollment_id}/',
            body, format='json',
        )


class ShownOnTheNextLessonOnlyTests(WalkInRegisterBase):
    def test_the_lesson_it_came_to_shows_it_plainly(self):
        self.add_walkin()
        row = self.row(OCC)
        self.assertIsNotNone(row)
        self.assertFalse(row['walk_in']['came_before'])
        self.assertFalse(row['walk_in']['last_time'])

    def test_the_next_lesson_says_it_came_last_time_and_that_this_is_the_last(self):
        self.add_walkin()
        row = self.row(NEXT)
        self.assertIsNotNone(row)
        self.assertTrue(row['walk_in']['came_before'])
        self.assertTrue(row['walk_in']['last_time'])
        self.assertEqual(row['walk_in']['last_seen'], str(OCC))

    def test_the_third_lesson_does_not_show_it(self):
        self.add_walkin()
        self.assertIsNone(self.row(THIRD))

    def test_coming_again_keeps_it_one_lesson_more(self):
        child_id = self.add_walkin().data['child_id']
        self.mark(child_id, NEXT)
        row = self.row(NEXT)
        self.assertFalse(row['walk_in']['came_before'])
        self.assertFalse(row['walk_in']['last_time'])
        third = self.row(THIRD)
        self.assertIsNotNone(third)
        self.assertTrue(third['walk_in']['last_time'])
        self.assertIsNone(self.row(THIRD + timedelta(weeks=1)))

    def test_taking_the_mark_back_takes_the_extra_lesson_back(self):
        child_id = self.add_walkin().data['child_id']
        self.mark(child_id, NEXT)
        self.mark(child_id, NEXT, 'not_marked')
        self.assertIsNone(self.row(THIRD))

    def test_a_cancelled_week_is_stepped_over(self):
        LessonCancellation.objects.create(lesson=self.lesson, occurrence_date=NEXT, reason='חג')
        self.add_walkin()
        row = self.row(THIRD)
        self.assertIsNotNone(row)
        self.assertTrue(row['walk_in']['last_time'])


class PhoneAfterwardsTests(WalkInRegisterBase):
    def test_the_instructor_adds_the_phone_later(self):
        added = self.add_walkin().data
        self.assertTrue(added['walk_in']['phone_missing'])
        res = self.update(added['id'], {'phone': '050-123-4567', 'date': str(OCC)})
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        self.assertFalse(res.data['walk_in']['phone_missing'])
        self.assertEqual(res.data['child_phone'], '0501234567')
        child = Child.objects.get(id=added['child_id'])
        self.assertEqual(child.phone_number, '0501234567')
        self.assertEqual(child.family.phone, '0501234567')

    def test_a_name_spelt_better(self):
        added = self.add_walkin(first='נעם', last='כהן').data
        res = self.update(added['id'], {'first_name': 'נועם', 'date': str(OCC)})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['child_name'], 'נועם כהן')

    def test_something_that_is_not_a_phone_is_refused(self):
        added = self.add_walkin().data
        self.assertEqual(self.update(added['id'], {'phone': '12'}).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.add_walkin(first='אחר', phone='abc12').status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_registered_child_cannot_be_changed_from_here(self):
        enrollment, _child, _family = self.real_child()
        res = self.update(enrollment.id, {'phone': '0501234567'})
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_same_name_tapped_again_with_a_phone_fills_it_in(self):
        self.add_walkin()
        res = self.add_walkin(phone='0501234567')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.data['walk_in']['phone_missing'])


class RemovedWithoutAPhoneTests(WalkInRegisterBase):
    def test_after_its_last_lesson_a_walk_in_with_no_phone_is_removed(self):
        added = self.add_walkin().data
        self.assertEqual(remove_expired_phoneless_ghosts(NEXT), 0)
        self.assertEqual(remove_expired_phoneless_ghosts(NEXT + timedelta(days=1)), 1)
        self.assertFalse(Child.objects.filter(id=added['child_id']).exists())
        self.assertFalse(LessonEnrollment.objects.filter(id=added['id']).exists())
        self.assertFalse(LessonAttendance.objects.filter(child_id=added['child_id']).exists())
        self.assertFalse(Family.objects.filter(name='כהן (נוסף בשיעור)').exists())

    def test_one_with_a_phone_stays_it_is_a_lead(self):
        added = self.add_walkin(phone='0501234567').data
        self.assertEqual(remove_expired_phoneless_ghosts(THIRD + timedelta(weeks=4)), 0)
        self.assertTrue(Child.objects.filter(id=added['child_id']).exists())

    def test_one_that_came_again_waits_for_its_new_last_lesson(self):
        added = self.add_walkin().data
        self.mark(added['child_id'], NEXT)
        self.assertEqual(remove_expired_phoneless_ghosts(NEXT + timedelta(days=1)), 0)
        self.assertEqual(remove_expired_phoneless_ghosts(THIRD + timedelta(days=1)), 1)

    def test_a_registered_child_is_never_touched(self):
        _enrollment, child, _family = self.real_child()
        remove_expired_phoneless_ghosts(THIRD + timedelta(weeks=10))
        self.assertTrue(Child.objects.filter(id=child.id).exists())


class InstructorToldBeforeTheEndTests(WalkInRegisterBase):
    def at(self, hour, minute):
        return timezone.make_aware(datetime(OCC.year, OCC.month, OCC.day, hour, minute))

    def run_at(self, hour, minute):
        with patch('apps.enrollments.register_reminders._send', return_value={'sent': True}) as send:
            summary = send_due_register_reminders(now=self.at(hour, minute))
        walk_in_calls = [c for c in send.call_args_list if c.args[0] == 'MANYCHAT_WALK_IN_PHONE_FLOW_NS']
        return summary, walk_in_calls

    def test_five_minutes_before_the_end_the_names_go_to_the_instructor(self):
        self.instructor.phone = '0529999999'
        self.instructor.save(update_fields=['phone'])
        self.add_walkin(first='נועם', last='כהן')
        self.add_walkin(first='דנה', last='לוי')
        _summary, calls = self.run_at(15, 50)
        self.assertEqual(calls, [])
        summary, calls = self.run_at(16, 55)
        self.assertEqual(summary['walk_in_phone_sent'], 1)
        fields = calls[0].kwargs['fields']
        self.assertEqual(fields['kogo_walkin_names'], 'דנה לוי, נועם כהן')
        self.assertEqual(fields['kogo_lesson_name'], 'חוג בדיקה 16:00')
        self.assertTrue(RegisterReminder.objects.filter(kind=RegisterReminder.KIND_WALK_IN_PHONE).exists())

    def test_said_once(self):
        self.add_walkin()
        self.run_at(16, 55)
        _summary, calls = self.run_at(17, 0)
        self.assertEqual(calls, [])

    def test_nothing_when_every_walk_in_has_a_phone(self):
        self.add_walkin(phone='0501234567')
        _summary, calls = self.run_at(16, 55)
        self.assertEqual(calls, [])

    def test_nothing_long_after_the_lesson(self):
        self.add_walkin()
        _summary, calls = self.run_at(18, 0)
        self.assertEqual(calls, [])


class OfficeSeesWhereItCameTests(WalkInRegisterBase):
    def test_the_card_says_which_lesson_and_when(self):
        from apps.core.models import UserProfile
        from apps.customers.tests.test_card_link import _user

        added = self.add_walkin().data
        self.mark(added['child_id'], NEXT)
        self.auth(_user(UserProfile.ROLE_MANAGER, 'office@walkin.test'))
        walk_in = self.card(added['child_id'], 'ghost')['walk_in']
        self.assertEqual(len(walk_in['lessons']), 1)
        lesson = walk_in['lessons'][0]
        self.assertEqual(lesson['course_name'], 'חוג בדיקה')
        self.assertEqual(lesson['first_seen'], str(OCC))
        self.assertEqual(lesson['last_seen'], str(NEXT))
        self.assertEqual(lesson['visits'], 2)
        self.assertEqual(lesson['instructor_name'], 'מורה בדיקה')
        self.assertTrue(walk_in['phone_missing'])

    def test_a_registered_child_has_no_walk_in_block(self):
        from apps.core.models import UserProfile
        from apps.customers.tests.test_card_link import _user

        _enrollment, child, _family = self.real_child()
        self.auth(_user(UserProfile.ROLE_MANAGER, 'office2@walkin.test'))
        self.assertIsNone(self.card(child.id, 'active')['walk_in'])

    def card(self, child_id, child_status):
        """The row the customers list hands the card — the card is drawn from it."""
        res = self.client.get(f'/api/v1/customers/children/?status={child_status}')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        rows = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        return next(row for row in rows if row['id'] == str(child_id))
