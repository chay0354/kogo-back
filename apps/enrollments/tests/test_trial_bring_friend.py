"""
"חבר מביא חבר" with the trial-day message (owner, 15.10.2026).

"כשאר נשלחת ההודעה לגבי האם הם מגיעים… המערכת תסרוק ותדע באיזה חוגים כמות
הרשומים הן מתחת ל15 ורק אז תאושר האוטומציה לשלוח את המודעה של החבר מביא חבר."

So: only after the 10:00 "are you coming" message went out for a trial, only
when the class the child is coming to has fewer than 15 registered (the number
the schedule shows), and only once.
"""
from datetime import datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import override_settings
from django.utils import timezone

from apps.core.manychat_service import ManyChatService
from apps.customers.models import Child
from apps.enrollments.bring_friend import registered_on_lesson
from apps.enrollments.models import LessonEnrollment
from apps.enrollments.tests.test_headcounts_exclude_trials import HeadcountTestBase
from apps.enrollments.trial_reminders import send_due_trial_reminders
from apps.external_students.models import ExternalStudent

JERUSALEM = ZoneInfo('Asia/Jerusalem')
BRING_FRIEND_NS = 'content_bring_friend'


def _flows(setting_name):
    return BRING_FRIEND_NS if setting_name == 'MANYCHAT_TRIAL_BRING_FRIEND_FLOW_NS' else ''


@override_settings(TIME_ZONE='Asia/Jerusalem', TRIAL_BRING_FRIEND_BELOW=15, TRIAL_BRING_FRIEND_ENABLED=True)
class BringFriendTests(HeadcountTestBase):
    def setUp(self):
        super().setUp()
        self.wednesday = self.the_next_wednesday()
        self.trial = self.enroll('ניסיון', child_status='trial_signed', trial_on=self.wednesday)

    @staticmethod
    def the_next_wednesday():
        from datetime import date
        today = date.today()
        return today + timedelta(days=(2 - today.weekday()) % 7 or 7)

    def students(self, count, *, status='active'):
        for i in range(count):
            self.enroll(f'תלמיד{status}{i}', child_status=status)

    def run_at(self, hour=10, minute=0, *, ten_am_sent=True, invitation_sent=True, flows=_flows):
        now = timezone.make_aware(datetime.combine(self.wednesday, time(hour, minute)), JERUSALEM)
        with patch('apps.enrollments.trial_reminders.timezone.localtime', return_value=now), \
             patch.object(ManyChatService, 'notify_registration', return_value={'sent': ten_am_sent}) as ten_am, \
             patch.object(ManyChatService, 'send_automation_to_contact',
                          return_value={'sent': invitation_sent}) as invitation, \
             patch.object(ManyChatService, 'resolve_flow_ns', side_effect=flows):
            summary = send_due_trial_reminders()
        return summary, ten_am, invitation

    def test_a_small_class_gets_the_invitation_right_after_the_10am_message(self):
        self.students(3)
        seen_ten_am_first = []

        def invitation_side_effect(**kwargs):
            self.trial.refresh_from_db()
            seen_ten_am_first.append(self.trial.trial_10am_reminder_sent_at is not None)
            return {'sent': True}

        now = timezone.make_aware(datetime.combine(self.wednesday, time(10, 0)), JERUSALEM)
        with patch('apps.enrollments.trial_reminders.timezone.localtime', return_value=now), \
             patch.object(ManyChatService, 'notify_registration', return_value={'sent': True}) as ten_am, \
             patch.object(ManyChatService, 'send_automation_to_contact',
                          side_effect=invitation_side_effect) as invitation, \
             patch.object(ManyChatService, 'resolve_flow_ns', side_effect=_flows):
            summary = send_due_trial_reminders()

        self.assertEqual(ten_am.call_count, 1)
        self.assertEqual(ten_am.call_args.kwargs['kind'], ManyChatService.REGISTRATION_KIND_TRIAL_10AM)
        invitation.assert_called_once_with(
            automation_type='flow', automation_id=BRING_FRIEND_NS, phone='0529999999', name='ניסיון',
        )
        self.assertEqual(seen_ten_am_first, [True])
        self.assertEqual(summary['bring_friend_sent'], 1)
        self.trial.refresh_from_db()
        self.assertIsNotNone(self.trial.trial_bring_friend_sent_at)

    def test_it_goes_once(self):
        self.students(3)
        self.run_at(10, 0)
        _, ten_am, invitation = self.run_at(10, 30)
        ten_am.assert_not_called()
        invitation.assert_not_called()

    def test_fifteen_registered_is_not_fewer_than_fifteen(self):
        self.students(15)
        summary, ten_am, invitation = self.run_at()
        self.assertEqual(ten_am.call_count, 1)
        invitation.assert_not_called()
        self.assertEqual(summary['bring_friend_full'], 1)
        self.trial.refresh_from_db()
        self.assertIsNone(self.trial.trial_bring_friend_sent_at)

    def test_fourteen_registered_gets_it(self):
        self.students(14)
        _, _, invitation = self.run_at()
        self.assertEqual(invitation.call_count, 1)

    def test_the_registered_are_counted_as_the_schedule_counts_them(self):
        # 12 פעיל + 1 בעיה באשראי + 1 בתהליך רישום = 14 registered. A child who
        # left and the other trials today are not registered, so 14 < 15.
        self.students(12)
        self.students(1, status='payment_problem')
        self.students(1, status='pending')
        self.students(3, status='inactive')
        for i in range(2):
            self.enroll(f'ניסיון נוסף{i}', child_status='trial_signed', trial_on=self.wednesday)
        summary, _, invitation = self.run_at()
        self.assertEqual(summary['bring_friend_full'], 0)
        self.assertEqual(invitation.call_count, 3)  # each trial family once

    def test_a_municipality_child_counts_as_registered(self):
        self.students(14)
        self.branch.is_external = True
        self.branch.save(update_fields=['is_external'])
        ExternalStudent.objects.create(lesson=self.lesson, first_name='ילד', last_name='עירייה', is_active=True)
        _, _, invitation = self.run_at()
        invitation.assert_not_called()

    @override_settings(TRIAL_BRING_FRIEND_BELOW=0)
    def test_zero_turns_it_off(self):
        self.students(1)
        _, ten_am, invitation = self.run_at()
        self.assertEqual(ten_am.call_count, 1)
        invitation.assert_not_called()

    def test_a_parent_who_said_remove_gets_no_invitation(self):
        from apps.core.models import WhatsAppOptOut

        self.students(3)
        WhatsAppOptOut.objects.create(phone='972529999999', opted_out_at=timezone.now(), source='message')
        summary, ten_am, invitation = self.run_at()
        self.assertEqual(ten_am.call_count, 1)  # the trial's own message still goes
        invitation.assert_not_called()
        self.assertEqual(summary['bring_friend_skipped'], 1)

    def test_an_unreadable_remove_list_sends_no_invitation(self):
        from apps.core import whatsapp_optout

        self.students(3)
        with patch('apps.core.whatsapp_optout.is_blocked', side_effect=whatsapp_optout.OptOutListUnavailable()):
            summary, _, invitation = self.run_at()
        invitation.assert_not_called()
        self.assertEqual(summary['bring_friend_errors'], 1)

    def test_no_10am_message_no_invitation(self):
        self.students(3)
        summary, ten_am, invitation = self.run_at(ten_am_sent=False)
        self.assertEqual(ten_am.call_count, 1)
        invitation.assert_not_called()
        self.assertEqual(summary['bring_friend_sent'], 0)

    def test_a_lesson_limited_to_six_that_has_six_is_full(self):
        self.lesson.capacity = 6
        self.lesson.save(update_fields=['capacity'])
        self.students(6)
        summary, _, invitation = self.run_at()
        invitation.assert_not_called()
        self.assertEqual(summary['bring_friend_full'], 1)

    def test_two_children_of_one_family_one_invitation(self):
        self.students(3)
        sibling = Child.objects.create(
            family=self.trial.child.family, first_name='אח', last_name='כהן',
            birth_date=self.trial.child.birth_date, gender='male', status='trial_signed',
        )
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=sibling, status='active', trial_lesson_date=self.wednesday,
        )
        summary, ten_am, invitation = self.run_at()
        self.assertEqual(ten_am.call_count, 2)
        self.assertEqual(invitation.call_count, 1)

    def test_a_failed_invitation_is_not_marked_sent_and_is_tried_again(self):
        self.students(3)
        summary, _, invitation = self.run_at(invitation_sent=False)
        self.assertEqual(invitation.call_count, 1)
        self.assertEqual(summary['bring_friend_errors'], 1)
        self.trial.refresh_from_db()
        self.assertIsNone(self.trial.trial_bring_friend_sent_at)

        summary, ten_am, invitation = self.run_at(10, 30)
        ten_am.assert_not_called()
        self.assertEqual(invitation.call_count, 1)
        self.assertEqual(summary['bring_friend_sent'], 1)

    def test_a_morning_message_sent_hours_ago_does_not_bring_a_late_invitation(self):
        # The 10:00 message went out before this code existed; the first run
        # after the deploy must not send invitations in the afternoon.
        self.students(3)
        LessonEnrollment.objects.filter(pk=self.trial.pk).update(
            trial_10am_reminder_sent_at=timezone.now() - timedelta(hours=3),
        )
        _, ten_am, invitation = self.run_at(13, 0)
        ten_am.assert_not_called()
        invitation.assert_not_called()

    def test_no_such_automation_in_manychat_sends_nothing_else(self):
        self.students(3)
        summary, ten_am, invitation = self.run_at(flows=lambda name: '')
        invitation.assert_not_called()
        self.assertEqual(ten_am.call_count, 1)  # the 10:00 message only — no free text in its place
        self.assertEqual(summary['bring_friend_skipped'], 1)

    def test_a_dry_run_sends_and_stamps_nothing(self):
        self.students(3)
        now = timezone.make_aware(datetime.combine(self.wednesday, time(10, 0)), JERUSALEM)
        with patch('apps.enrollments.trial_reminders.timezone.localtime', return_value=now), \
             patch.object(ManyChatService, 'notify_registration') as ten_am, \
             patch.object(ManyChatService, 'send_automation_to_contact') as invitation, \
             patch.object(ManyChatService, 'resolve_flow_ns', side_effect=_flows):
            summary = send_due_trial_reminders(dry_run=True)
        ten_am.assert_not_called()
        invitation.assert_not_called()
        self.assertEqual(summary['bring_friend_sent'], 1)
        self.trial.refresh_from_db()
        self.assertIsNone(self.trial.trial_bring_friend_sent_at)

    def test_moving_the_trial_to_another_date_clears_the_invitation(self):
        LessonEnrollment.objects.filter(pk=self.trial.pk).update(trial_bring_friend_sent_at=timezone.now())
        self.trial.refresh_from_db()
        from apps.enrollments.serializers import LessonEnrollmentSerializer

        serializer = LessonEnrollmentSerializer(
            self.trial, data={'trial_lesson_date': (self.wednesday + timedelta(days=7)).isoformat()}, partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.trial.refresh_from_db()
        self.assertIsNone(self.trial.trial_bring_friend_sent_at)


@override_settings(TIME_ZONE='Asia/Jerusalem')
class SwitchedOffTests(HeadcountTestBase):
    """
    Owner, 15.10.2026, the evening it went live: "זה ישלח את הפרטים של ראש העין
    לכל שאר הסניפים אז תבטל את האוטומציה הזאת".
    """

    def test_off_unless_switched_on(self):
        wednesday = BringFriendTests.the_next_wednesday()
        trial = self.enroll('ניסיון', child_status='trial_signed', trial_on=wednesday)
        now = timezone.make_aware(datetime.combine(wednesday, time(10, 0)), JERUSALEM)
        with patch('apps.enrollments.trial_reminders.timezone.localtime', return_value=now), \
             patch.object(ManyChatService, 'notify_registration', return_value={'sent': True}) as ten_am, \
             patch.object(ManyChatService, 'send_automation_to_contact') as invitation, \
             patch.object(ManyChatService, 'resolve_flow_ns', side_effect=_flows):
            summary = send_due_trial_reminders()
        self.assertEqual(ten_am.call_count, 1)  # the 10:00 message goes as before
        invitation.assert_not_called()
        self.assertEqual(summary['bring_friend_sent'], 0)
        trial.refresh_from_db()
        self.assertIsNone(trial.trial_bring_friend_sent_at)

    def test_the_rosh_haayin_automation_is_never_found_by_its_name(self):
        self.assertNotIn('MANYCHAT_TRIAL_BRING_FRIEND_FLOW_NS', ManyChatService._FLOW_NAME_ALIASES)
        flows = [{'name': 'חבר מביא חבר', 'ns': 'content20260927102922_555377'}]
        with override_settings(MANYCHAT_TRIAL_BRING_FRIEND_FLOW_NS=''), \
             patch.object(ManyChatService, 'get_flows', return_value=flows):
            self.assertEqual(ManyChatService(api_key='x').resolve_flow_ns('MANYCHAT_TRIAL_BRING_FRIEND_FLOW_NS'), '')


@override_settings(TIME_ZONE='Asia/Jerusalem')
class RegisteredCountIsTheScheduleNumberTests(HeadcountTestBase):
    """The invitation reads the very number the office sees as "רשומים"."""

    def test_same_number_as_the_calendar(self):
        wednesday = BringFriendTests.the_next_wednesday()
        for i in range(3):
            self.enroll(f'פעיל{i}')
        self.enroll('כרטיס נכשל', child_status='payment_problem')
        self.enroll('לא שילם עדיין', child_status='pending')
        self.enroll('עזב ולא נסגר', child_status='inactive')
        self.enroll('ניסיון ישן בלי תאריך', child_status='trial_completed')
        self.enroll('ניסיון היום', child_status='trial_signed', trial_on=wednesday)
        self.branch.is_external = True
        self.branch.save(update_fields=['is_external'])
        ExternalStudent.objects.create(lesson=self.lesson, first_name='ילד', last_name='עירייה', is_active=True)

        res = self.client.get(
            '/api/v1/scheduling/lessons/',
            {'start_date': wednesday.isoformat(), 'end_date': wednesday.isoformat()},
        )
        self.assertEqual(res.status_code, 200, res.data)
        row = next(item for item in res.data if item['id'] == str(self.lesson.id))
        self.assertEqual(row['active_student_count'], 6)
        self.assertEqual(registered_on_lesson(self.lesson, wednesday), row['active_student_count'])
