"""Broadcast from the customers list: lesson/day filters, select-all ids, and the send itself."""
from datetime import date, time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.broadcast import broadcast_to_children
from apps.customers.models import Child, Family, Parent
from apps.enrollments.models import LessonEnrollment

IDS_URL = '/api/v1/customers/children/ids/'
LIST_URL = '/api/v1/customers/children/'
BROADCAST_URL = '/api/v1/customers/children/broadcast/'


def _user(role, username):
    User = get_user_model()
    user = User.objects.create_user(username=username, email=username, password='x')
    UserProfile.objects.update_or_create(user=user, defaults={'role': role})
    return user


def _client_for(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
    return client


def _lesson(course, room, *, day_of_week, start=time(17, 0)):
    return Lesson.objects.create(
        course=course, room=room, day_of_week=day_of_week,
        start_time=start, end_time=time(start.hour + 1, 0), is_recurring=True,
    )


def _child(family, first_name, *, status='active'):
    return Child.objects.create(
        family=family, first_name=first_name, last_name=family.name,
        birth_date=date(2016, 1, 1), gender='male', status=status,
    )


def _enroll(child, lesson, status='active'):
    return LessonEnrollment.objects.create(child=child, lesson=lesson, status=status, start_date=date(2026, 9, 1))


class _Studio(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='Main')
        self.other_branch = Branch.objects.create(name='Other')
        room = Room.objects.create(branch=self.branch, name='A', capacity=20)
        other_room = Room.objects.create(branch=self.other_branch, name='B', capacity=20)
        ctype = CourseType.objects.create(name='Dance')
        self.course = Course.objects.create(course_type=ctype, name='Dance', price=300, capacity=10, branch=self.branch)
        self.other_course = Course.objects.create(course_type=ctype, name='Judo', price=300, capacity=10, branch=self.other_branch)
        self.sunday = _lesson(self.course, room, day_of_week=0)
        self.tuesday = _lesson(self.course, room, day_of_week=2)
        self.other_sunday = _lesson(self.other_course, other_room, day_of_week=0)

        self.cohen = Family.objects.create(name='Cohen', phone='0501111111', branch=self.branch)
        Parent.objects.create(family=self.cohen, first_name='Dana', last_name='Cohen', phone='0501111111', is_primary=True)
        self.levi = Family.objects.create(name='Levi', phone='0502222222', branch=self.branch)
        Parent.objects.create(family=self.levi, first_name='Rina', last_name='Levi', phone='0502222222', is_primary=True)
        self.mizrahi = Family.objects.create(name='Mizrahi', phone='0503333333', branch=self.other_branch)
        Parent.objects.create(family=self.mizrahi, first_name='Gal', last_name='Mizrahi', phone='0503333333', is_primary=True)

        self.noa = _child(self.cohen, 'Noa')          # Sunday
        self.ido = _child(self.cohen, 'Ido')          # Tuesday — same phone as Noa
        self.tom = _child(self.levi, 'Tom')           # Sunday
        self.gal = _child(self.mizrahi, 'Gal')        # other branch, Sunday
        _enroll(self.noa, self.sunday)
        _enroll(self.ido, self.tuesday)
        _enroll(self.tom, self.sunday)
        _enroll(self.gal, self.other_sunday)

        self.manager = _user(UserProfile.ROLE_MANAGER, 'm@test.com')
        self.client = _client_for(self.manager)


class LessonAndDayFiltersTest(_Studio):
    def test_lesson_filter_returns_that_slot_only(self):
        res = self.client.get(LIST_URL, {'lesson': str(self.sunday.id)})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual({r['first_name'] for r in res.data['results']}, {'Noa', 'Tom'})

    def test_day_filter_spans_branches(self):
        res = self.client.get(LIST_URL, {'day_of_week': '0'})
        self.assertEqual({r['first_name'] for r in res.data['results']}, {'Noa', 'Tom', 'Gal'})

    def test_invalid_day_is_ignored_and_invalid_lesson_is_empty(self):
        res = self.client.get(LIST_URL, {'day_of_week': 'sunday'})
        self.assertEqual(res.data['count'], 4)
        res = self.client.get(LIST_URL, {'lesson': 'nope'})
        self.assertEqual(res.data['count'], 0)

    def test_inactive_enrollment_does_not_match(self):
        LessonEnrollment.objects.filter(child=self.tom).update(status='inactive')
        res = self.client.get(LIST_URL, {'lesson': str(self.sunday.id)})
        self.assertEqual({r['first_name'] for r in res.data['results']}, {'Noa'})


class SelectAllIdsTest(_Studio):
    def test_ids_follow_the_filters_and_the_search(self):
        res = self.client.get(IDS_URL, {'day_of_week': '0'})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(set(res.data['ids']), {str(self.noa.id), str(self.tom.id), str(self.gal.id)})
        self.assertEqual(res.data['count'], 3)
        self.assertFalse(res.data['capped'])
        res = self.client.get(IDS_URL, {'day_of_week': '0', 'search': 'Tom'})
        self.assertEqual(res.data['ids'], [str(self.tom.id)])

    def test_ids_match_the_list_count(self):
        listed = self.client.get(LIST_URL, {'branch': str(self.branch.id)}).data['count']
        ids = self.client.get(IDS_URL, {'branch': str(self.branch.id)}).data
        self.assertEqual(ids['count'], listed)

    def test_partner_only_gets_their_branch(self):
        partner = _user(UserProfile.ROLE_PARTNER, 'p@test.com')
        partner.profile.assigned_branches.add(self.branch)
        res = _client_for(partner).get(IDS_URL)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertNotIn(str(self.gal.id), res.data['ids'])
        self.assertIn(str(self.noa.id), res.data['ids'])

    def test_weaker_duplicate_card_is_collapsed(self):
        # A leftover pending card of the same child (same name, same family) must not be selected twice.
        ghost = _child(self.cohen, 'Noa', status='pending')
        res = self.client.get(IDS_URL, {'search': 'Noa'})
        self.assertEqual(res.data['ids'], [str(self.noa.id)])
        self.assertNotIn(str(ghost.id), res.data['ids'])


class BroadcastEndpointTest(_Studio):
    def setUp(self):
        super().setUp()
        cache.clear()

    def _post(self, client=None, **body):
        payload = {
            'child_ids': [str(self.noa.id), str(self.ido.id), str(self.tom.id)],
            'automation_type': 'kind',
            'automation_id': 'subscription',
        }
        payload.update(body)
        return (client or self.client).post(BROADCAST_URL, payload, format='json')

    def test_partner_and_worker_are_refused(self):
        partner = _user(UserProfile.ROLE_PARTNER, 'p@test.com')
        partner.profile.assigned_branches.add(self.branch)
        self.assertEqual(self._post(_client_for(partner)).status_code, 403)
        worker = _user(UserProfile.ROLE_WORKER, 'w@test.com')
        self.assertEqual(self._post(_client_for(worker)).status_code, 403)

    def test_dry_run_is_the_default_and_sends_nothing(self):
        with patch('apps.customers.broadcast.ManyChatService.notify_registration') as send, \
             patch('apps.customers.broadcast.ManyChatService.send_automation_to_contact') as flow:
            res = self._post()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data['dry_run'])
        send.assert_not_called()
        flow.assert_not_called()
        self.assertEqual(res.data['preview_count'], 2)   # Noa + Tom; Ido shares Noa's phone
        self.assertEqual(res.data['skipped'], 1)
        by_name = {r['child_name']: r for r in res.data['results']}
        self.assertEqual(by_name['Ido Cohen']['reason'], 'duplicate_phone')
        self.assertEqual(by_name['Noa Cohen']['status'], 'preview')
        self.assertEqual(sorted(res.data['phones']), sorted(['972501111111', '972502222222']))

    def test_skip_phones_from_the_previous_chunk_are_honoured(self):
        res = self._post(child_ids=[str(self.tom.id)], skip_phones=['0502222222'])
        self.assertEqual(res.data['skipped'], 1)
        self.assertEqual(res.data['results'][0]['reason'], 'duplicate_phone')

    def test_kind_goes_out_with_the_childs_own_lesson(self):
        with patch('apps.customers.views.ManyChatService.is_configured', new=property(lambda self: True)), \
             patch('apps.customers.broadcast.ManyChatService.notify_registration',
                   return_value={'sent': True, 'method': 'flow'}) as send:
            res = self._post(child_ids=[str(self.noa.id)], dry_run=False)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['sent'], 1)
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs['kind'], 'subscription')
        self.assertEqual(kwargs['child_name'], 'Noa Cohen')
        self.assertEqual(kwargs['course_name'], 'Dance')
        self.assertEqual(kwargs['day_name'], 'ראשון')
        self.assertEqual(kwargs['start_time'], '17:00')
        self.assertEqual(kwargs['branch_name'], 'Main')
        self.assertEqual(res.data['results'][0]['method'], 'flow')

    def test_flow_goes_through_send_automation_to_contact(self):
        with patch('apps.customers.views.ManyChatService.is_configured', new=property(lambda self: True)), \
             patch('apps.customers.broadcast.ManyChatService.send_automation_to_contact',
                   return_value={'sent': True, 'method': 'flow'}) as flow:
            res = self._post(child_ids=[str(self.tom.id)], automation_type='flow',
                             automation_id='content2026_x', dry_run=False)
        self.assertEqual(res.data['sent'], 1)
        kwargs = flow.call_args.kwargs
        self.assertEqual(kwargs['automation_id'], 'content2026_x')
        self.assertEqual(kwargs['name'], 'Rina Levi')
        self.assertEqual(kwargs['phone'], '0502222222')

    def test_a_failed_send_is_reported_not_raised(self):
        with patch('apps.customers.views.ManyChatService.is_configured', new=property(lambda self: True)), \
             patch('apps.customers.broadcast.ManyChatService.notify_registration',
                   return_value={'sent': False, 'reason': 'lookup_failed'}):
            res = self._post(child_ids=[str(self.tom.id)], dry_run=False)
        self.assertEqual(res.data['failed'], 1)
        self.assertEqual(res.data['results'][0]['error'], 'lookup_failed')

    def test_no_phone_and_no_lesson_are_skipped_with_reasons(self):
        silent = Family.objects.create(name='Silent', phone='', branch=self.branch)
        mute = _child(silent, 'Mute')
        _enroll(mute, self.sunday)
        idle = _child(self.levi, 'Idle')          # phone, but no active lesson
        res = self._post(child_ids=[str(mute.id), str(idle.id)])
        by_name = {r['child_name']: r['reason'] for r in res.data['results']}
        self.assertEqual(by_name['Mute Silent'], 'no_parent_phone')
        self.assertEqual(by_name['Idle Levi'], 'no_active_lesson')

    def test_a_flow_still_reaches_a_child_without_a_lesson(self):
        idle = _child(self.levi, 'Idle')
        res = self._post(child_ids=[str(idle.id)], automation_type='flow', automation_id='content2026_x')
        self.assertEqual(res.data['preview_count'], 1)

    def test_unconfigured_manychat_blocks_a_real_send_only(self):
        with patch('apps.customers.views.ManyChatService.is_configured', new=property(lambda self: False)):
            self.assertEqual(self._post().status_code, 200)
            self.assertEqual(self._post(dry_run=False).status_code, 400)

    def test_validation(self):
        self.assertEqual(self._post(automation_type='sms').status_code, 400)
        self.assertEqual(self._post(automation_id='not-a-kind').status_code, 400)
        self.assertEqual(self._post(child_ids=[]).status_code, 400)
        self.assertEqual(self._post(child_ids=['nope']).status_code, 400)
        self.assertEqual(self._post(child_ids=[str(self.noa.id)] * 26).status_code, 400)

    def test_throttled_after_the_rate(self):
        with patch.object(ScopedRateThrottle, 'THROTTLE_RATES', {'customers_broadcast': '2/min'}):
            self.assertEqual(self._post().status_code, 200)
            self.assertEqual(self._post().status_code, 200)
            self.assertEqual(self._post().status_code, 429)


class BroadcastServiceTest(_Studio):
    def test_an_exception_from_manychat_marks_the_row_failed(self):
        from apps.core.manychat_service import ManyChatError, ManyChatService

        class Boom(ManyChatService):
            def notify_registration(self, **kwargs):
                raise ManyChatError('down')

        out = broadcast_to_children([self.noa], automation_type='kind', automation_id='trial',
                                    dry_run=False, service=Boom())
        self.assertEqual(out['failed'], 1)
        self.assertEqual(out['results'][0]['error'], 'down')


class ReviewFixesTest(_Studio):
    def setUp(self):
        super().setUp()
        cache.clear()

    def _post(self, **body):
        payload = {'child_ids': [str(self.noa.id)], 'automation_type': 'kind', 'automation_id': 'subscription'}
        payload.update(body)
        return self.client.post(BROADCAST_URL, payload, format='json')

    def test_only_an_explicit_false_turns_the_preview_off(self):
        with patch('apps.customers.views.ManyChatService.is_configured', new=property(lambda self: True)), \
             patch('apps.customers.broadcast.ManyChatService.notify_registration', return_value={'sent': True}) as send:
            for garbage in (None, 0, '', [], 'nope'):
                res = self._post(dry_run=garbage)
                self.assertEqual(res.status_code, 200, res.content)
                self.assertTrue(res.data['dry_run'], garbage)
            send.assert_not_called()
            self.assertFalse(self._post(dry_run=False).data['dry_run'])
            self.assertFalse(self._post(dry_run='false').data['dry_run'])
            self.assertEqual(send.call_count, 2)

    def test_a_payments_problem_enrollment_still_gets_the_message(self):
        LessonEnrollment.objects.filter(child=self.noa).update(status='payments_problem')
        res = self._post(automation_id='payment_failed')
        self.assertEqual(res.data['preview_count'], 1)
        res = self.client.get(LIST_URL, {'lesson': str(self.sunday.id)})
        self.assertIn('Noa', {r['first_name'] for r in res.data['results']})

    def test_the_lesson_the_audience_was_filtered_by_wins(self):
        _enroll(self.noa, self.tuesday)          # Noa is now Sunday + Tuesday
        LessonEnrollment.objects.filter(child=self.noa, lesson=self.tuesday).update(start_date=date(2025, 1, 1))
        with patch('apps.customers.views.ManyChatService.is_configured', new=property(lambda self: True)), \
             patch('apps.customers.broadcast.ManyChatService.notify_registration', return_value={'sent': True}) as send:
            self._post(dry_run=False)
            self.assertEqual(send.call_args.kwargs['day_name'], 'שלישי')   # oldest enrollment wins by default
            self._post(dry_run=False, lesson_id=str(self.sunday.id))
            self.assertEqual(send.call_args.kwargs['day_name'], 'ראשון')
            self._post(dry_run=False, day_of_week=0)
            self.assertEqual(send.call_args.kwargs['day_name'], 'ראשון')

    def test_a_failed_send_does_not_silence_the_sibling(self):
        with patch('apps.customers.views.ManyChatService.is_configured', new=property(lambda self: True)), \
             patch('apps.customers.broadcast.ManyChatService.notify_registration',
                   side_effect=[{'sent': False, 'reason': 'lookup_failed'}, {'sent': True}]) as send:
            res = self._post(child_ids=[str(self.noa.id), str(self.ido.id)], dry_run=False)
        self.assertEqual(send.call_count, 2)
        self.assertEqual([r['status'] for r in res.data['results']], ['failed', 'sent'])
        self.assertEqual(res.data['phones'], ['972501111111'])
