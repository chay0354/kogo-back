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

    def test_the_form_is_told_it_is_on_and_given_a_ticket(self):
        answer = self.client.get(IDENTIFY).json()

        self.assertTrue(answer['enabled'])
        self.assertTrue(answer['ticket'])

    def test_a_family_that_never_accepted_the_paragraph_is_not_recognised(self):
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
        self.assertEqual(self.outcomes(), ['known', 'known', 'device', 'device'])

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

    def test_an_email_the_identified_parent_retyped_is_kept(self):
        self.client.post(REGISTER, self._hidden(parent_email='new@example.com'), format='json')

        self.family.refresh_from_db()
        self.assertEqual(self.family.email, 'new@example.com')

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

    def test_a_child_of_another_family_cannot_be_chosen(self):
        _, other = _family('222222226', '0502222222', child_name='נועה')

        response = self.client.post(REGISTER, self._hidden(identified_child_id=str(other.id)), format='json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Payment.objects.count(), 0)

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

    def test_a_card_with_no_phone_takes_the_one_typed(self):
        Family.objects.filter(id=self.family.id).update(phone='')
        self.family.parents.update(phone='')

        self._register(parent_phone='0529999999')

        self.family.refresh_from_db()
        self.assertEqual(self.family.phone, '0529999999')


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
