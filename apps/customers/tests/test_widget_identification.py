"""The registration form recognising a returning parent — and everything that keeps it from being a lookup service.

What is held here: who is recognised and who is not, that a refusal looks
exactly like "not known", the limits, what leaves the server (one character of
each detail), and that a registration with the token is completed from the
family's card and never overwrites it.
"""
import time
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.models import RegistrationTerms
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers import widget_identification as identification
from apps.customers.identification_models import FamilyIdentificationSwitch, WidgetIdentifyAttempt
from apps.customers.models import Child, Family, Payment

IDENTIFY = '/api/v1/customers/widget/identify/'
REGISTER = '/api/v1/customers/widget/register/'
QUOTE = '/api/v1/customers/widget/quote/'

PARENT_ID = '123456782'
PHONE = '0501234567'
UNKNOWN = {'status': 'unknown'}


def _ticket(age=30):
    return signing.dumps({'t': time.time() - age}, salt=identification.FORM_SALT)


def _family(parent_id=PARENT_ID, phone=PHONE, *, child_name='מאיה', consent=True, status='active'):
    family = TestDataFactory.create_family(
        name='כהן', phone=phone, email='dana.cohen@example.com', parent_id_number=parent_id,
        widget_identification_consent_at=timezone.now() if consent else None,
    )
    TestDataFactory.create_parent(
        family=family, first_name='דנה', last_name='כהן', phone=phone, email='dana.cohen@example.com',
    )
    child = TestDataFactory.create_child(
        family=family, first_name=child_name, last_name='כהן', id_number='218847366',
        birth_date=date(2018, 6, 21), gender='female', status=status,
    )
    return family, child


@override_settings(WIDGET_IDENTIFICATION_ENABLED=True, REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
@patch.object(identification, 'MIN_ANSWER_SECONDS', 0)
class IdentificationCase(TestCase):
    def setUp(self):
        cache.clear()  # the soft per-minute throttle counts across tests
        self.client = APIClient()
        self.alert = patch.object(identification, '_alert_office').start()
        self.addCleanup(patch.stopall)
        self.family, self.child = _family()

    def ask(self, parent_id=PARENT_ID, phone=PHONE, *, device='device-aaaaaaaaaaaaaaaa', **extra):
        body = {'parent_id_number': parent_id, 'parent_phone': phone, 'device_id': device, 'ticket': _ticket()}
        body.update(extra)
        return self.client.post(IDENTIFY, body, format='json').json()

    def outcomes(self):
        return list(WidgetIdentifyAttempt.objects.order_by('created_at').values_list('outcome', flat=True))


class WhoIsRecognised(IdentificationCase):
    def test_both_details_right_is_a_known_parent(self):
        answer = self.ask()

        self.assertEqual(answer['status'], 'known')
        self.assertTrue(answer['token'])
        self.assertEqual(self.outcomes(), ['known'])

    def test_only_one_character_of_each_stored_detail_leaves_the_server(self):
        answer = self.ask()

        self.assertEqual(answer['parent'], {
            'first_name': 'ד••••', 'last_name': 'כ••••', 'email': 'd•••••••••', 'phone': '•••••••••7',
        })
        self.assertEqual(answer['children'], [{
            'id': str(self.child.id), 'first_name': 'מאיה', 'last_name': 'כ••••',
            'id_number': '••••••••6', 'birth_date': '••/••/•••8', 'gender': 'female',
        }])
        body = str(answer)
        for secret in ('דנה', 'dana.cohen', '218847366', '2018', PHONE):
            self.assertNotIn(secret, body)

    def test_a_ghost_child_is_not_listed(self):
        TestDataFactory.create_child(family=self.family, first_name='רפאים', status='ghost')

        self.assertEqual([child['first_name'] for child in self.ask()['children']], ['מאיה'])

    def test_a_wrong_phone_and_an_unknown_number_get_the_same_answer(self):
        self.assertEqual(self.ask(phone='0529999999'), UNKNOWN)
        self.assertEqual(self.ask(parent_id='222222226'), UNKNOWN)
        self.assertEqual(self.outcomes(), ['mismatch', 'unknown'])

    def test_a_phone_stored_with_dashes_or_country_code_still_matches(self):
        Family.objects.filter(id=self.family.id).update(phone='+972-50-1234567')
        self.family.parents.update(phone='050-123-4567')

        self.assertEqual(self.ask()['status'], 'known')

    def test_off_means_nobody_is_known_and_nothing_is_kept(self):
        with override_settings(WIDGET_IDENTIFICATION_ENABLED=False):
            self.assertEqual(self.client.get(IDENTIFY).json(), {'enabled': False, 'ticket': ''})
            self.assertEqual(self.ask(), UNKNOWN)
        self.assertEqual(self.outcomes(), [])

    def test_the_repositorys_own_secret_key_keeps_it_off(self):
        """A token is only as good as the key that signs it; the default key is public."""
        with override_settings(SECRET_KEY_IS_DEFAULT=True):
            self.assertEqual(self.client.get(IDENTIFY).json(), {'enabled': False, 'ticket': ''})
            self.assertEqual(self.ask(), UNKNOWN)

    def test_the_form_is_told_it_is_on_and_given_a_ticket(self):
        answer = self.client.get(IDENTIFY).json()

        self.assertTrue(answer['enabled'])
        self.assertTrue(answer['ticket'])

    def test_a_family_that_was_with_us_before_the_paragraph_is_recognised(self):
        """Whoever was already a customer never signed the new terms — and is whom the form is for."""
        Family.objects.filter(id=self.family.id).update(widget_identification_consent_at=None)

        self.assertEqual(self.ask()['status'], 'known')

    @override_settings(WIDGET_IDENTIFICATION_REQUIRES_CONSENT=True)
    def test_asking_for_the_consent_first_is_a_switch(self):
        Family.objects.filter(id=self.family.id).update(widget_identification_consent_at=None)

        self.assertEqual(self.ask(), UNKNOWN)
        self.assertEqual(self.outcomes(), ['no_consent'])

    def test_a_family_the_office_switched_off_is_not_recognised(self):
        Family.objects.filter(id=self.family.id).update(widget_identification_blocked_at=timezone.now())

        self.assertEqual(self.ask(), UNKNOWN)
        self.assertEqual(self.outcomes(), ['hidden'])

    def test_a_family_gone_for_over_a_year_is_not_recognised(self):
        Child.objects.filter(id=self.child.id).update(status='inactive')

        self.assertEqual(self.ask(), UNKNOWN)
        self.assertEqual(self.outcomes(), ['old'])

    def test_a_payment_in_the_last_year_keeps_a_family_recognised(self):
        Child.objects.filter(id=self.child.id).update(status='inactive')
        Payment.objects.create(
            child=self.child, family=self.family, payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('300'), final_amount=Decimal('300'),
            payment_date=timezone.now() - timedelta(days=200),
        )

        self.assertEqual(self.ask()['status'], 'known')

    def test_a_trial_lesson_in_the_last_year_keeps_a_family_recognised(self):
        """Back from a trial lesson to register: the parent the identification is there for."""
        from apps.enrollments.models import LessonEnrollment

        Child.objects.filter(id=self.child.id).update(status='trial_completed')
        lesson = TestDataFactory.create_lesson(course=TestDataFactory.create_course(), day_of_week=0)
        LessonEnrollment.objects.create(
            child=self.child, lesson=lesson, status='active',
            trial_lesson_date=timezone.localdate() - timedelta(days=20),
        )

        self.assertEqual(self.ask()['status'], 'known')

    def test_a_trial_lesson_over_a_year_ago_does_not(self):
        from apps.enrollments.models import LessonEnrollment

        Child.objects.filter(id=self.child.id).update(status='trial_completed')
        lesson = TestDataFactory.create_lesson(course=TestDataFactory.create_course(), day_of_week=0)
        LessonEnrollment.objects.create(
            child=self.child, lesson=lesson, status='active',
            trial_lesson_date=timezone.localdate() - timedelta(days=400),
        )

        self.assertEqual(self.ask(), UNKNOWN)
        self.assertEqual(self.outcomes(), ['old'])

    def test_two_cards_with_one_identity_number_show_neither(self):
        _family(phone='0507777777', child_name='אחר')

        self.assertEqual(self.ask(), UNKNOWN)
        self.assertEqual(self.outcomes(), ['duplicate'])


class ASimilarPhone(IdentificationCase):
    def test_a_digit_or_two_off_offers_the_stored_number_by_its_last_digit_only(self):
        answer = self.ask(phone='0501234576')

        self.assertEqual(answer['status'], 'near')
        self.assertEqual(answer['last_digit'], '7')
        self.assertEqual(set(answer), {'status', 'last_digit', 'near_token'})

    def test_accepting_the_offer_identifies_the_parent(self):
        offered = self.ask(phone='0501234576')

        answer = self.client.post(IDENTIFY, {
            'near_token': offered['near_token'], 'device_id': 'device-aaaaaaaaaaaaaaaa', 'ticket': _ticket(),
        }, format='json').json()

        self.assertEqual(answer['status'], 'known')
        self.assertEqual(answer['parent']['phone'], '•••••••••7')
        self.assertEqual(self.outcomes(), ['near', 'known_near'])

    def test_three_digits_off_is_another_number(self):
        self.assertEqual(self.ask(phone='0501234999'), UNKNOWN)

    def test_only_a_slip_of_the_finger_is_similar(self):
        """One wrong digit, or two neighbours swapped — not any two wrong digits."""
        near = identification._near
        self.assertTrue(near('0501234568', '0501234567'))   # one digit
        self.assertTrue(near('0501234576', '0501234567'))   # neighbours swapped
        self.assertFalse(near('0501234589', '0501234567'))  # two wrong digits side by side
        self.assertFalse(near('0511234568', '0501234567'))  # two wrong digits apart
        self.assertFalse(near('0501234567', '0501234567'))  # the number itself is not "similar"
        self.assertFalse(near('050123456', '0501234567'))

    def test_two_wrong_digits_are_not_offered_the_stored_number(self):
        self.assertEqual(self.ask(phone='0511234568'), UNKNOWN)
        self.assertEqual(self.outcomes(), ['mismatch'])

    def test_an_offer_is_taken_up_only_on_the_device_it_was_made_on(self):
        offered = self.ask(phone='0501234576')

        answer = self.client.post(IDENTIFY, {
            'near_token': offered['near_token'], 'device_id': 'device-bbbbbbbbbbbbbbbb', 'ticket': _ticket(),
        }, format='json').json()

        self.assertEqual(answer, UNKNOWN)

    def test_a_forged_offer_is_not_accepted(self):
        answer = self.client.post(IDENTIFY, {
            'near_token': 'made-up', 'device_id': 'device-aaaaaaaaaaaaaaaa', 'ticket': _ticket(),
        }, format='json').json()

        self.assertEqual(answer, UNKNOWN)


class TheLimits(IdentificationCase):
    def test_five_wrong_phones_lock_the_identity_number_even_for_the_right_one(self):
        for last in range(5):
            self.assertEqual(self.ask(phone=f'052999990{last}'), UNKNOWN)

        self.assertEqual(self.ask(), UNKNOWN)
        self.assertEqual(self.outcomes()[-1], 'locked')
        self.alert.assert_called_once()

    def test_a_similar_phone_counts_as_a_wrong_try(self):
        for last in ('0', '1', '2', '3', '4'):
            self.ask(phone=f'050123456{last}')

        self.assertEqual(self.ask(), UNKNOWN)

    def test_a_third_family_from_one_device_stops_identification_there(self):
        _family('222222226', '0502222222', child_name='נועה')
        _family('333333334', '0503333333', child_name='איתי')

        self.assertEqual(self.ask()['status'], 'known')
        self.assertEqual(self.ask('222222226', '0502222222')['status'], 'known')
        self.assertEqual(self.ask('333333334', '0503333333'), UNKNOWN)
        self.alert.assert_called_once()
        # For a day, on that device, the earlier two are not shown again either.
        self.assertEqual(self.ask(), UNKNOWN)
        self.assertEqual(self.outcomes(), ['known', 'known', 'device', 'device_held'])

    def test_being_refused_again_does_not_start_the_day_over(self):
        _family('222222226', '0502222222', child_name='נועה')
        _family('333333334', '0503333333', child_name='איתי')
        self.ask()
        self.ask('222222226', '0502222222')
        self.ask('333333334', '0503333333')
        # The block is a day old: the tries made while it held do not extend it.
        WidgetIdentifyAttempt.objects.filter(outcome='device').update(
            created_at=timezone.now() - timedelta(hours=25),
        )
        WidgetIdentifyAttempt.objects.filter(outcome='known').update(
            created_at=timezone.now() - timedelta(hours=25),
        )
        self.ask()
        WidgetIdentifyAttempt.objects.filter(outcome='device_held').delete()

        self.assertEqual(self.ask()['status'], 'known')

    def test_another_device_is_not_affected(self):
        _family('222222226', '0502222222', child_name='נועה')
        _family('333333334', '0503333333', child_name='איתי')
        self.ask()
        self.ask('222222226', '0502222222')
        self.ask('333333334', '0503333333')

        self.assertEqual(self.ask(device='device-bbbbbbbbbbbbbbbb')['status'], 'known')

    def test_the_same_parent_again_is_not_another_family(self):
        for _ in range(4):
            self.assertEqual(self.ask()['status'], 'known')

    def test_too_many_families_in_an_hour_stop_it_for_everyone(self):
        _family('222222226', '0502222222', child_name='נועה')
        _family('333333334', '0503333333', child_name='איתי')
        with patch.object(identification, 'HOURLY_CAP', 2):
            self.ask()
            self.ask('222222226', '0502222222', device='device-bbbbbbbbbbbbbbbb')

            self.assertEqual(self.ask('333333334', '0503333333', device='device-cccccccccccccccc'), UNKNOWN)
        self.assertEqual(self.outcomes()[-1], 'cap')

    def test_too_many_families_in_a_day_stop_it_too(self):
        _family('222222226', '0502222222', child_name='נועה')
        with patch.object(identification, 'DAILY_CAP', 1):
            self.ask()

            self.assertEqual(self.ask('222222226', '0502222222', device='device-bbbbbbbbbbbbbbbb'), UNKNOWN)
        self.assertEqual(self.outcomes()[-1], 'cap')

    def test_one_parent_asking_again_and_again_does_not_use_up_the_cap(self):
        with patch.object(identification, 'HOURLY_CAP', 2):
            for _ in range(5):
                self.assertEqual(self.ask()['status'], 'known')

    def test_a_network_is_stopped_only_for_families_not_yet_seen_from_it(self):
        _family('222222226', '0502222222', child_name='נועה')
        with patch.object(identification, 'NETWORK_FAMILY_LIMIT', 1):
            self.assertEqual(self.ask()['status'], 'known')
            self.assertEqual(self.ask('222222226', '0502222222', device='device-bbbbbbbbbbbbbbbb'), UNKNOWN)
            self.assertEqual(self.ask(device='device-cccccccccccccccc')['status'], 'known')
        self.assertIn('network', self.outcomes())


class TheSilentCheck(IdentificationCase):
    def test_a_request_without_the_forms_ticket_is_not_answered(self):
        self.assertEqual(self.ask(ticket=''), UNKNOWN)
        self.assertEqual(self.ask(ticket='made-up'), UNKNOWN)
        self.assertEqual(self.outcomes(), ['bot', 'bot'])

    def test_a_form_opened_an_instant_ago_is_not_answered(self):
        self.assertEqual(self.ask(ticket=_ticket(age=0)), UNKNOWN)

    def test_the_hidden_field_filled_is_not_answered(self):
        self.assertEqual(self.ask(website='http://x'), UNKNOWN)

    def test_no_device_id_is_not_answered(self):
        self.assertEqual(self.ask(device=''), UNKNOWN)

    def test_a_malformed_number_is_not_even_counted(self):
        self.assertEqual(self.ask(parent_id='123456789'), UNKNOWN)
        self.assertEqual(self.ask(phone='031234567'), UNKNOWN)
        self.assertEqual(self.outcomes(), [])


@override_settings(WIDGET_IDENTIFICATION_NOTICE_ENABLED=True, MANYCHAT_IDENTIFICATION_NOTICE_FLOW_NS='content123')
class TheNoticeToTheParent(IdentificationCase):
    def _service(self, subscriber={'id': 77}):
        service = MagicMock()
        service.is_configured = True
        service.find_existing.return_value = subscriber
        return service

    def test_the_parent_is_told_and_the_form_may_say_so(self):
        service = self._service()
        with patch('apps.core.manychat_service.ManyChatService', return_value=service):
            answer = self.ask()

        self.assertTrue(answer['notice_sent'])
        service.send_flow.assert_called_once_with(77, 'content123')
        service.find_existing.assert_called_once_with(PHONE)

    def test_a_second_identification_minutes_later_does_not_write_again(self):
        service = self._service()
        with patch('apps.core.manychat_service.ManyChatService', return_value=service):
            self.ask()
            again = self.ask()

        self.assertTrue(again['notice_sent'])
        service.send_flow.assert_called_once()

    def test_a_parent_we_cannot_reach_is_identified_and_the_form_does_not_claim_a_message(self):
        with patch('apps.core.manychat_service.ManyChatService', return_value=self._service(subscriber=None)):
            answer = self.ask()

        self.assertEqual(answer['status'], 'known')
        self.assertFalse(answer['notice_sent'])

    def test_nobody_is_written_to_while_the_notice_is_off(self):
        service = self._service()
        with override_settings(WIDGET_IDENTIFICATION_NOTICE_ENABLED=False), \
                patch('apps.core.manychat_service.ManyChatService', return_value=service):
            answer = self.ask()

        self.assertFalse(answer['notice_sent'])
        service.send_flow.assert_not_called()

    def test_a_refused_parent_is_never_written_to(self):
        service = self._service()
        with patch('apps.core.manychat_service.ManyChatService', return_value=service):
            self.ask(phone='0529999999')

        service.send_flow.assert_not_called()


class RegistrationWithTheToken(IdentificationCase):
    def setUp(self):
        super().setUp()
        self.course = TestDataFactory.create_course(price=Decimal('350.00'))
        self.lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.token = self.ask()['token']

    def _hidden(self, **overrides):
        """What the form sends for a parent it identified: the hidden details empty."""
        body = {
            'identify_token': self.token, 'identified_child_id': str(self.child.id),
            'device_id': 'device-aaaaaaaaaaaaaaaa',
            'parent_id_number': PARENT_ID, 'parent_phone': '', 'parent_first_name': '', 'parent_last_name': '',
            'parent_email': '', 'child_first_name': 'מאיה', 'child_last_name': '', 'child_id_number': '',
            'child_birth_date': '', 'child_gender': '',
            'course_id': str(self.course.id), 'lesson_id': str(self.lesson.id),
        }
        body.update(overrides)
        return body

    def test_the_hidden_details_are_completed_from_the_card(self):
        response = self.client.post(REGISTER, self._hidden(), format='json')

        self.assertEqual(response.status_code, 201, response.content)
        payment = Payment.objects.get(id=response.json()['payment_id'])
        self.assertEqual(payment.child_id, self.child.id)
        self.assertEqual(Child.objects.filter(family=self.family).count(), 1)
        self.assertEqual(Family.objects.count(), 1)

    def test_the_card_is_left_as_it_was(self):
        self.client.post(REGISTER, self._hidden(), format='json')

        self.family.refresh_from_db()
        self.child.refresh_from_db()
        self.assertEqual((self.family.phone, self.family.email), (PHONE, 'dana.cohen@example.com'))
        self.assertEqual((self.child.last_name, self.child.id_number), ('כהן', '218847366'))

    def test_a_typed_detail_of_a_chosen_child_never_overwrites_the_child(self):
        self.client.post(REGISTER, self._hidden(child_last_name='אחר', child_id_number='345678903'), format='json')

        self.child.refresh_from_db()
        self.assertEqual((self.child.last_name, self.child.id_number), ('כהן', '218847366'))
        self.assertEqual(Child.objects.filter(family=self.family).count(), 1)

    def test_an_email_the_identified_parent_retyped_is_kept_and_the_office_is_told(self):
        with patch('apps.customers.widget_views._tell_office_of_new_email') as told:
            self.client.post(REGISTER, self._hidden(parent_email='new@example.com'), format='json')

        self.family.refresh_from_db()
        self.assertEqual(self.family.email, 'new@example.com')
        told.assert_called_once_with(self.family, 'dana.cohen@example.com', 'new@example.com')

    def test_another_child_is_added_to_the_same_family(self):
        response = self.client.post(REGISTER, self._hidden(
            identified_child_id='', child_first_name='יובל', child_last_name='כהן',
            child_id_number='345678903', child_birth_date='2019-01-15', child_gender='male',
        ), format='json')

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Family.objects.count(), 1)
        self.assertEqual(
            sorted(Child.objects.filter(family=self.family).values_list('first_name', flat=True)),
            ['יובל', 'מאיה'],
        )

    def test_the_quote_takes_the_token_too(self):
        response = self.client.post(QUOTE, {'items': [self._hidden()]}, format='json')

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['items'][0]['base_amount'], 350.0)
        self.assertEqual(Payment.objects.count(), 0)

    def test_a_token_that_is_not_good_is_refused_and_says_so(self):
        for token in ('made-up', self.token[:-3] + 'abc'):
            response = self.client.post(REGISTER, self._hidden(identify_token=token), format='json')

            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.json()['identification_expired'])
        self.assertEqual(Payment.objects.count(), 0)

    def test_a_token_taken_to_another_device_is_worth_nothing(self):
        for device in ('device-bbbbbbbbbbbbbbbb', ''):
            response = self.client.post(REGISTER, self._hidden(device_id=device), format='json')

            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.json()['identification_expired'])
        self.assertEqual(Payment.objects.count(), 0)

    def test_a_chosen_child_that_is_not_an_id_is_refused_not_an_error(self):
        response = self.client.post(REGISTER, self._hidden(identified_child_id='not-an-id'), format='json')

        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()['identification_expired'])

    def test_a_child_of_another_family_cannot_be_chosen(self):
        _, other = _family('222222226', '0502222222', child_name='נועה')

        response = self.client.post(REGISTER, self._hidden(identified_child_id=str(other.id)), format='json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Payment.objects.count(), 0)

    def test_a_token_speaks_for_its_own_family_only(self):
        """One family's token in front of another family's identity number is not that family's."""
        _family('222222226', '0502222222', child_name='נועה')

        response = self.client.post(
            REGISTER, self._hidden(parent_id_number='222222226', identified_child_id=''), format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()['identification_expired'])

    def test_a_token_stops_working_once_the_office_switches_the_family_off(self):
        Family.objects.filter(id=self.family.id).update(widget_identification_blocked_at=timezone.now())

        response = self.client.post(REGISTER, self._hidden(), format='json')

        self.assertEqual(response.status_code, 400)

    def test_a_token_is_worth_nothing_while_identification_is_off(self):
        with override_settings(WIDGET_IDENTIFICATION_ENABLED=False):
            response = self.client.post(REGISTER, self._hidden(), format='json')

        self.assertEqual(response.status_code, 400)


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class AnIdentityNumberAloneChangesNothing(TestCase):
    """Typing a parent's identity number used to be enough to replace the family's phone and email."""

    def setUp(self):
        self.client = APIClient()
        self.family, self.child = _family()
        self.course = TestDataFactory.create_course(price=Decimal('350.00'))
        self.lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.told = patch('apps.customers.widget_views._tell_office_of_other_contact').start()
        self.addCleanup(patch.stopall)

    def _register(self, **overrides):
        body = {
            'parent_id_number': PARENT_ID, 'parent_first_name': 'מישהו', 'parent_last_name': 'אחר',
            'parent_phone': PHONE, 'parent_email': 'dana.cohen@example.com',
            'child_first_name': 'יובל', 'child_last_name': 'כהן', 'child_id_number': '345678903',
            'child_birth_date': '2019-01-15', 'child_gender': 'male',
            'course_id': str(self.course.id), 'lesson_id': str(self.lesson.id),
        }
        body.update(overrides)
        return self.client.post(REGISTER, body, format='json')

    def test_another_phone_does_not_replace_the_familys_contact_details(self):
        response = self._register(parent_phone='0529999999', parent_email='thief@example.com')

        self.assertEqual(response.status_code, 201, response.content)
        self.family.refresh_from_db()
        primary = self.family.parents.get(is_primary=True)
        self.assertEqual((self.family.phone, self.family.email), (PHONE, 'dana.cohen@example.com'))
        self.assertEqual((primary.phone, primary.email), (PHONE, 'dana.cohen@example.com'))
        self.told.assert_called_once()

    def test_the_familys_own_phone_may_update_the_email(self):
        self._register(parent_email='new@example.com')

        self.family.refresh_from_db()
        self.assertEqual(self.family.email, 'new@example.com')
        self.told.assert_not_called()

    def test_a_card_with_no_phone_is_not_given_one_by_an_identity_number(self):
        """Otherwise: type the number, set a phone, come back with it — and be the parent."""
        Family.objects.filter(id=self.family.id).update(phone='')
        self.family.parents.update(phone='')

        self.assertEqual(self._register(parent_phone='0529999999').status_code, 201)

        self.family.refresh_from_db()
        self.assertEqual(self.family.phone, '')
        self.told.assert_called_once()

    def test_what_the_office_is_told_carries_a_phone_and_an_email_and_nothing_else(self):
        from apps.customers.widget_views import _typed_contact

        self.assertEqual(_typed_contact('052-999 9999', 'a@b.co'), 'טלפון 0529999999, דוא״ל a@b.co')
        self.assertEqual(_typed_contact('התקשרו עכשיו ל-0529999999 דחוף', '<b>x</b>'), 'טלפון 0529999999')
        self.assertEqual(_typed_contact('', 'x' * 200 + '@b.co'), 'פרטי קשר אחרים')

    def test_a_placeholder_phone_on_the_card_proves_nobody(self):
        """Zeros on a card are a phone anybody can guess."""
        for placeholder in ('0000000000', '0500000000', '0', '03-1234567'):
            Family.objects.filter(id=self.family.id).update(phone=placeholder)
            self.family.parents.update(phone=placeholder)
            self.family.refresh_from_db()
            self.assertFalse(identification.proves_parent(self.family, placeholder), placeholder)

    def test_the_answer_is_the_same_whichever_phone_was_typed(self):
        """No refusal that the right phone escapes: that would say which phone is right."""
        right = self._register(child_first_name='א', child_id_number='456789017')
        wrong = self._register(parent_phone='0529999999', child_first_name='ב', child_id_number='567890124')

        self.assertEqual((right.status_code, wrong.status_code), (201, 201))
        self.assertEqual(set(right.json()), set(wrong.json()))


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class ACardNobodyPaidOnYet(TestCase):
    """
    Whoever types an identity number first opens the card. Until the family
    pays, the next registration corrects the card — as it always did — so a
    stranger who got there first is not left holding the family's messages.
    """

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.course = TestDataFactory.create_course(price=Decimal('350.00'))
        self.lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.told = patch('apps.customers.widget_views._tell_office_of_other_contact').start()
        self.addCleanup(patch.stopall)

    def _register(self, **overrides):
        body = {
            'parent_id_number': PARENT_ID, 'parent_first_name': 'דנה', 'parent_last_name': 'כהן',
            'parent_phone': PHONE, 'parent_email': 'dana.cohen@example.com',
            'child_first_name': 'מאיה', 'child_last_name': 'כהן', 'child_id_number': '218847366',
            'child_birth_date': '2018-06-21', 'child_gender': 'female',
            'course_id': str(self.course.id), 'lesson_id': str(self.lesson.id),
            'computerized_docs_consent': True,
        }
        body.update(overrides)
        return self.client.post(REGISTER, body, format='json')

    def test_the_real_parent_takes_the_card_back_from_whoever_typed_it_first(self):
        self._register(parent_phone='0529999999', parent_email='stranger@example.com',
                       child_first_name='בדוי', child_id_number='345678903')

        self.assertEqual(self._register().status_code, 201)

        family = Family.objects.get(parent_id_number=PARENT_ID)
        self.assertEqual((family.phone, family.email), (PHONE, 'dana.cohen@example.com'))
        self.told.assert_not_called()

    def test_once_the_family_paid_the_card_is_its_own(self):
        first = self._register()
        Payment.objects.filter(id=first.json()['payment_id']).update(status='completed', payment_date=timezone.now())

        self._register(parent_phone='0529999999', child_first_name='אחר', child_id_number='345678903')

        self.assertEqual(Family.objects.get(parent_id_number=PARENT_ID).phone, PHONE)
        self.told.assert_called_once()

    def test_taking_a_card_over_is_not_a_consent_to_be_identified(self):
        self._register(computerized_docs_consent=False)

        self._register(parent_phone='0529999999', child_first_name='אחר', child_id_number='345678903')

        family = Family.objects.get(parent_id_number=PARENT_ID)
        self.assertEqual(family.phone, '0529999999')
        self.assertIsNone(family.widget_identification_consent_at)

    def _trial(self, child_id):
        from apps.enrollments.models import LessonEnrollment

        LessonEnrollment.objects.create(
            child_id=child_id, lesson=self.lesson, status='active', trial_lesson_date=timezone.localdate(),
        )

    def _identify(self, phone=PHONE):
        with patch.object(identification, 'MIN_ANSWER_SECONDS', 0):
            return self.client.post(IDENTIFY, {
                'parent_id_number': PARENT_ID, 'parent_phone': phone,
                'device_id': 'device-aaaaaaaaaaaaaaaa', 'ticket': _ticket(),
            }, format='json').json()

    @override_settings(WIDGET_IDENTIFICATION_ENABLED=True)
    def test_a_family_back_from_a_trial_lesson_is_recognised_by_its_own_phone(self):
        self._trial(self._register().json()['child_id'])

        answer = self._identify()

        self.assertEqual(answer['status'], 'known')
        self.assertEqual([child['first_name'] for child in answer['children']], ['מאיה'])

    @override_settings(WIDGET_IDENTIFICATION_ENABLED=True)
    def test_a_phone_an_identity_number_alone_put_on_the_card_recognises_nobody(self):
        """Else anybody with a parent's identity number puts his phone on the card and is shown the children."""
        self._trial(self._register().json()['child_id'])

        self._register(parent_phone='0529999999', child_first_name='אחר', child_id_number='345678903')

        family = Family.objects.get(parent_id_number=PARENT_ID)
        self.assertEqual(family.phone, '0529999999')
        self.assertIsNotNone(family.widget_contact_unproven_at)
        self.assertEqual(self._identify('0529999999'), UNKNOWN)
        self.assertEqual(self._identify(PHONE), UNKNOWN)
        self.assertEqual(
            list(WidgetIdentifyAttempt.objects.order_by('created_at').values_list('outcome', flat=True)),
            ['unproven_card', 'unproven_card'],
        )

    @override_settings(WIDGET_IDENTIFICATION_ENABLED=True)
    def test_the_parent_typing_the_cards_own_phone_again_leaves_no_mark(self):
        self._trial(self._register().json()['child_id'])

        self._register(child_first_name='נועם', child_id_number='345678903')

        self.assertIsNone(Family.objects.get(parent_id_number=PARENT_ID).widget_contact_unproven_at)
        self.assertEqual(self._identify()['status'], 'known')

    @override_settings(WIDGET_IDENTIFICATION_ENABLED=True)
    def test_once_that_family_has_paid_its_card_is_its_own_again(self):
        self._trial(self._register().json()['child_id'])
        taken = self._register(parent_phone='0529999999', child_first_name='אחר', child_id_number='345678903')
        Payment.objects.filter(id=taken.json()['payment_id']).update(status='completed', payment_date=timezone.now())

        self.assertEqual(self._identify('0529999999')['status'], 'known')

    @override_settings(WIDGET_IDENTIFICATION_ENABLED=True)
    def test_a_registration_never_paid_and_no_trial_is_not_a_customer_yet(self):
        self._register()

        self.assertEqual(self._identify(), UNKNOWN)


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class TheConsent(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.course = TestDataFactory.create_course(price=Decimal('350.00'))
        self.lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)

    def _register(self, **overrides):
        body = {
            'parent_id_number': PARENT_ID, 'parent_first_name': 'דנה', 'parent_last_name': 'כהן',
            'parent_phone': PHONE, 'parent_email': 'dana.cohen@example.com',
            'child_first_name': 'מאיה', 'child_last_name': 'כהן', 'child_id_number': '218847366',
            'child_birth_date': '2018-06-21', 'child_gender': 'female',
            'course_id': str(self.course.id), 'lesson_id': str(self.lesson.id),
            'computerized_docs_consent': True,
        }
        body.update(overrides)
        return self.client.post(REGISTER, body, format='json')

    def test_accepting_terms_that_carry_the_paragraph_is_the_consent(self):
        self.assertEqual(self._register().status_code, 201)

        self.assertIsNotNone(Family.objects.get(parent_id_number=PARENT_ID).widget_identification_consent_at)

    def test_terms_without_the_paragraph_record_nothing(self):
        RegistrationTerms.objects.update_or_create(pk=1, defaults={'content': '<p>תקנון ישן</p>'})

        self.assertEqual(self._register().status_code, 201)

        self.assertIsNone(Family.objects.get(parent_id_number=PARENT_ID).widget_identification_consent_at)

    def test_a_registration_that_did_not_accept_the_terms_records_nothing(self):
        self.assertEqual(self._register(computerized_docs_consent=False).status_code, 201)

        self.assertIsNone(Family.objects.get(parent_id_number=PARENT_ID).widget_identification_consent_at)

    def test_an_identity_number_alone_cannot_give_a_familys_consent(self):
        """It would open the family to identification on a stranger's say-so."""
        self.assertEqual(self._register(computerized_docs_consent=False).status_code, 201)

        response = self._register(
            parent_phone='0529999999', child_first_name='אחר', child_id_number='345678903',
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertIsNone(Family.objects.get(parent_id_number=PARENT_ID).widget_identification_consent_at)

    def test_the_parent_with_the_cards_phone_gives_it_on_a_later_registration(self):
        self.assertEqual(self._register(computerized_docs_consent=False).status_code, 201)

        self._register(child_first_name='אחר', child_id_number='345678903')

        self.assertIsNotNone(Family.objects.get(parent_id_number=PARENT_ID).widget_identification_consent_at)

    def test_a_quote_records_nothing(self):
        body = self._register  # noqa: F841 — the payload builder only
        response = self.client.post(QUOTE, {'items': [{
            'parent_id_number': PARENT_ID, 'parent_first_name': 'דנה', 'parent_last_name': 'כהן',
            'parent_phone': PHONE, 'child_first_name': 'מאיה', 'child_last_name': 'כהן',
            'child_id_number': '218847366', 'child_birth_date': '2018-06-21', 'child_gender': 'female',
            'course_id': str(self.course.id), 'lesson_id': str(self.lesson.id), 'computerized_docs_consent': True,
        }]}, format='json')

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Family.objects.count(), 0)


class TheOfficeSwitch(TestCase):
    def setUp(self):
        self.family, _ = _family()
        self.url = f'/api/v1/customers/families/{self.family.id}/widget-identification/'
        # Read again: the instance create_user returns still carries the profile its signal made.
        self.manager = get_user_model().objects.get(pk=TestDataFactory.create_user('office@example.com').pk)
        self.client = APIClient()
        self.client.force_authenticate(self.manager)

    def test_switching_off_needs_a_reason(self):
        response = self.client.post(self.url, {'blocked': True, 'reason': ''}, format='json')

        self.assertEqual(response.status_code, 400)
        self.family.refresh_from_db()
        self.assertIsNone(self.family.widget_identification_blocked_at)

    def test_switching_off_is_kept_with_who_when_and_why(self):
        response = self.client.post(self.url, {'blocked': True, 'reason': 'בקשת ההורה'}, format='json')

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body['blocked'])
        self.assertEqual(body['reason'], 'בקשת ההורה')
        self.assertEqual(len(body['history']), 1)
        self.assertEqual(body['history'][0]['reason'], 'בקשת ההורה')
        row = FamilyIdentificationSwitch.objects.get()
        self.assertEqual((row.family_id, row.blocked, row.changed_by), (self.family.id, True, self.manager))

    def test_switching_back_on_needs_a_reason_too_and_keeps_both_rows(self):
        self.client.post(self.url, {'blocked': True, 'reason': 'בקשת ההורה'}, format='json')

        self.assertEqual(self.client.post(self.url, {'blocked': False, 'reason': ''}, format='json').status_code, 400)
        response = self.client.post(self.url, {'blocked': False, 'reason': 'ההורה ביקש להחזיר'}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['blocked'])
        self.assertEqual([row['blocked'] for row in response.json()['history']], [False, True])

    def test_the_same_state_twice_is_refused(self):
        response = self.client.post(self.url, {'blocked': False, 'reason': 'סתם'}, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(FamilyIdentificationSwitch.objects.count(), 0)

    def test_a_value_that_is_not_true_or_false_changes_nothing(self):
        response = self.client.post(self.url, {'blocked': 'true', 'reason': 'בקשת ההורה'}, format='json')

        self.assertEqual(response.status_code, 400)

    def test_reading_shows_the_state_and_the_consent(self):
        body = self.client.get(self.url).json()

        self.assertFalse(body['blocked'])
        self.assertIsNotNone(body['consent_at'])
        self.assertEqual(body['history'], [])

    def test_a_partner_may_switch_off_but_only_a_manager_switches_back_on(self):
        from apps.core.models import UserProfile

        partner_user = TestDataFactory.create_user('partner@example.com', role=UserProfile.ROLE_PARTNER)
        profile = UserProfile.objects.get(user=partner_user)
        profile.assigned_branches.add(self.family.branch)
        partner = APIClient()
        partner.force_authenticate(get_user_model().objects.get(pk=partner_user.pk))

        off = partner.post(self.url, {'blocked': True, 'reason': 'בקשת ההורה'}, format='json')
        on = partner.post(self.url, {'blocked': False, 'reason': 'סתם'}, format='json')

        self.assertEqual(off.status_code, 200, off.content)
        self.assertEqual(on.status_code, 403)
        self.family.refresh_from_db()
        self.assertIsNotNone(self.family.widget_identification_blocked_at)
        self.assertEqual(
            self.client.post(self.url, {'blocked': False, 'reason': 'בדיקה'}, format='json').status_code, 200,
        )

    def test_the_reason_is_not_in_every_list_of_families(self):
        self.client.post(self.url, {'blocked': True, 'reason': 'צו הרחקה'}, format='json')

        listed = self.client.get(f'/api/v1/customers/families/{self.family.id}/').json()

        self.assertNotIn('widget_identification_blocked_reason', listed)
        self.assertNotIn('צו הרחקה', str(listed))
        self.assertIsNotNone(listed['widget_identification_blocked_at'])

    def test_an_instructor_cannot_touch_it(self):
        from apps.core.models import UserProfile

        client = APIClient()
        coach = TestDataFactory.create_user('coach@example.com', role=UserProfile.ROLE_WORKER)
        client.force_authenticate(get_user_model().objects.get(pk=coach.pk))

        self.assertEqual(client.get(self.url).status_code, 403)
        self.assertEqual(client.post(self.url, {'blocked': True, 'reason': 'x'}, format='json').status_code, 403)

    def test_nobody_signed_out_can_touch_it(self):
        self.assertIn(APIClient().get(self.url).status_code, (401, 403))


class TheMasks(TestCase):
    def test_a_detail_the_card_does_not_hold_is_shown_as_nothing(self):
        self.assertEqual(identification.mask_text(''), '')
        self.assertEqual(identification.mask_email(None), '')
        self.assertEqual(identification.mask_number(''), '')
        self.assertEqual(identification.mask_date(None), '')

    def test_one_character_and_dots(self):
        self.assertEqual(identification.mask_text(' דנה '), 'ד••••')
        self.assertEqual(identification.mask_email('dana@example.com'), 'd•••••••••')
        self.assertEqual(identification.mask_number('050-1234567', dots=9), '•••••••••7')
        self.assertEqual(identification.mask_date(date(2018, 6, 21)), '••/••/•••8')


EARLIER_KEY = 'the-key-that-was-in-the-repository'
DEVICE = 'device-aaaaaaaaaaaaaaaa'


@override_settings(
    SECRET_KEY='a-real-key-set-on-the-server', SECRET_KEY_IS_DEFAULT=False, SECRET_KEY_FALLBACKS=[EARLIER_KEY],
)
class AfterARealKeyIsSet(IdentificationCase):
    """Links already sent keep opening with the earlier key; an identification never does."""

    def test_a_ticket_signed_with_the_earlier_key_opens_nothing(self):
        ticket = signing.dumps({'t': time.time() - 30}, key=EARLIER_KEY, salt=identification.FORM_SALT)

        self.assertEqual(self.ask(ticket=ticket), UNKNOWN)
        self.assertEqual(self.ask()['status'], 'known')

    def test_a_token_signed_with_the_earlier_key_is_nobody(self):
        token = self.ask()['token']
        forged = signing.dumps(
            signing.loads(token, salt=identification.TOKEN_SALT), key=EARLIER_KEY, salt=identification.TOKEN_SALT,
        )

        self.assertEqual(identification.family_of_token(token, DEVICE), self.family)
        self.assertIsNone(identification.family_of_token(forged, DEVICE))

    def test_a_similar_number_offer_signed_with_the_earlier_key_identifies_nobody(self):
        offered = self.ask(phone='0501234576')['near_token']
        forged = signing.dumps(
            signing.loads(offered, salt=identification.NEAR_SALT), key=EARLIER_KEY, salt=identification.NEAR_SALT,
        )

        answer = self.client.post(
            IDENTIFY, {'near_token': forged, 'device_id': DEVICE, 'ticket': _ticket()}, format='json',
        ).json()

        self.assertEqual(answer, UNKNOWN)

    def test_a_card_link_a_parent_already_holds_still_opens(self):
        from apps.customers import card_replacement

        sent_before = signing.dumps(
            {'f': str(self.family.id)}, key=EARLIER_KEY, salt=card_replacement.SIGN_SALT,
        ).replace(':', '~')
        sent_now = card_replacement.build_family_token(self.family)

        self.assertEqual(card_replacement.resolve_family_token(sent_before), self.family)
        # A new link is signed with the real key: it opens with no earlier key at all.
        with override_settings(SECRET_KEY_FALLBACKS=[]):
            self.assertEqual(card_replacement.resolve_family_token(sent_now), self.family)
            with self.assertRaises(card_replacement.CardReplacementError):
                card_replacement.resolve_family_token(sent_before)


@override_settings(WIDGET_IDENTIFICATION_ENABLED=True)
@patch.object(identification, 'MIN_ANSWER_SECONDS', 0)
@patch('apps.enrollments.trial_reminders.stamp_and_notify_trial_enrollment', return_value={'sent': False})
class BackFromAFreeTrialLesson(TestCase):
    """The parent the identification is there for: booked a free trial on the form, now comes to register."""

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        patch.object(identification, '_alert_office').start()
        self.addCleanup(patch.stopall)
        self.course = TestDataFactory.create_course(price=Decimal('320.00'))
        today = timezone.localdate()
        self.trial_day = today + timedelta(days=((2 - today.weekday()) % 7 or 7))
        # Lesson days count from Sunday: 3 is a Wednesday, which is weekday() 2.
        self.lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=3)

    def _book_the_trial(self, phone=PHONE):
        return self.client.post('/api/v1/customers/widget/trial-register/', {
            'parent_id_number': PARENT_ID, 'parent_first_name': 'דנה', 'parent_last_name': 'כהן',
            'parent_phone': phone,
            'child_first_name': 'מאיה', 'child_last_name': 'כהן', 'child_id_number': '218847366',
            'child_birth_date': '2018-06-21', 'child_gender': 'female',
            'course_id': str(self.course.id), 'lesson_id': str(self.lesson.id),
            'trial_lesson_date': self.trial_day.isoformat(),
        }, format='json')

    def _identify(self, phone=PHONE):
        return self.client.post(IDENTIFY, {
            'parent_id_number': PARENT_ID, 'parent_phone': phone,
            'device_id': 'device-aaaaaaaaaaaaaaaa', 'ticket': _ticket(),
        }, format='json').json()

    def test_the_form_recognises_them(self, _notify):
        self.assertEqual(self._book_the_trial().status_code, 201)

        answer = self._identify()

        self.assertEqual(answer['status'], 'known')
        self.assertEqual([child['first_name'] for child in answer['children']], ['מאיה'])

    def test_not_by_another_phone(self, _notify):
        self._book_the_trial()

        self.assertEqual(self._identify('0529999999'), UNKNOWN)
