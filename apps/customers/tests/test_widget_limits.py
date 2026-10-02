"""The registration form's open endpoints: a count that holds, and answers that give nothing away."""
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers import widget_limits
from apps.customers.identification_models import WidgetRateBucket
from apps.customers.models import Family

LOOKUP = '/api/v1/customers/widget/lookup/'
QUOTE = '/api/v1/customers/widget/quote/'
REGISTER = '/api/v1/customers/widget/register/'
IDENTIFY = '/api/v1/customers/widget/identify/'


def _payload(**overrides):
    base = {
        'parent_id_number': '123456782', 'parent_first_name': 'Test', 'parent_last_name': 'Parent',
        'parent_phone': '0501234567', 'parent_email': 'parent@example.com',
        'child_first_name': 'Kid', 'child_last_name': 'Parent', 'child_id_number': '234567892',
        'child_birth_date': '2015-01-01', 'child_gender': 'male',
    }
    base.update(overrides)
    return base


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class WidgetLimitsTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.course = TestDataFactory.create_course(price=Decimal('350.00'))
        self.lesson = TestDataFactory.create_lesson(course=self.course, day_of_week=0)

    def _lookup(self, **extra):
        return self.client.post(LOOKUP, {
            'parent_id_number': '123456782', 'child_first_name': 'Kid', 'child_last_name': 'Parent',
        }, format='json', **extra)

    def _item(self, **overrides):
        return _payload(course_id=str(self.course.id), lesson_id=str(self.lesson.id), **overrides)

    def test_lookups_from_one_address_stop_at_the_hourly_count(self):
        with patch('apps.customers.widget_views.WIDGET_LOOKUP_HOURLY_LIMIT', 2):
            self.assertEqual(self._lookup().status_code, 200)
            self.assertEqual(self._lookup().status_code, 200)
            refused = self._lookup()

        self.assertEqual(refused.status_code, 429)
        self.assertEqual(refused.json(), {'error': widget_limits.TOO_MANY_MESSAGE})

    def test_another_address_is_counted_on_its_own(self):
        with patch('apps.customers.widget_views.WIDGET_LOOKUP_HOURLY_LIMIT', 1):
            self.assertEqual(self._lookup().status_code, 200)
            self.assertEqual(self._lookup().status_code, 429)
            self.assertEqual(self._lookup(HTTP_X_FORWARDED_FOR='203.0.113.9').status_code, 200)

    def test_quotes_from_one_address_stop_at_the_hourly_count(self):
        with patch('apps.customers.widget_views.WIDGET_QUOTE_HOURLY_LIMIT', 1):
            self.assertEqual(self.client.post(QUOTE, {'items': [self._item()]}, format='json').status_code, 200)
            self.assertEqual(self.client.post(QUOTE, {'items': [self._item()]}, format='json').status_code, 429)

    def test_registration_itself_is_never_stopped_by_the_look_up_and_quote_counts(self):
        with patch('apps.customers.widget_views.WIDGET_LOOKUP_HOURLY_LIMIT', 0), \
                patch('apps.customers.widget_views.WIDGET_QUOTE_HOURLY_LIMIT', 0):
            self.assertEqual(self._lookup().status_code, 429)
            response = self.client.post(REGISTER, self._item(), format='json')

        self.assertEqual(response.status_code, 201, response.content)

    def test_the_address_is_kept_only_as_a_keyed_hash(self):
        self._lookup(HTTP_X_FORWARDED_FOR='203.0.113.9')

        bucket = WidgetRateBucket.objects.get()
        self.assertEqual((bucket.scope, bucket.count), ('lookup', 1))
        self.assertNotIn('203.0.113.9', bucket.key_hash)
        self.assertEqual(len(bucket.key_hash), 64)

    def test_a_limiter_that_cannot_count_lets_the_request_by(self):
        with patch.object(WidgetRateBucket.objects, 'get_or_create', side_effect=RuntimeError('db down')):
            self.assertFalse(widget_limits.over_hourly_limit('lookup', '203.0.113.9', 0))

    def test_an_identification_over_the_count_gets_the_same_not_known(self):
        with override_settings(WIDGET_IDENTIFICATION_ENABLED=True), \
                patch('apps.customers.widget_identify_views.IDENTIFY_HOURLY_LIMIT', 0):
            response = self.client.post(IDENTIFY, {'parent_id_number': '123456782'}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'unknown'})

    def test_a_quote_and_an_identification_are_never_stored_by_a_browser_or_a_proxy(self):
        quote = self.client.post(QUOTE, {'items': [self._item()]}, format='json')

        self.assertEqual(quote['Cache-Control'], 'no-store')
        self.assertEqual(self.client.get(IDENTIFY)['Cache-Control'], 'no-store')
        self.assertEqual(self.client.post(IDENTIFY, {}, format='json')['Cache-Control'], 'no-store')

    def test_the_count_can_run_by_day_and_by_more_than_one(self):
        self.assertFalse(widget_limits.over_limit('names', '123456782', 10, per='day', by=6))
        self.assertTrue(widget_limits.over_limit('names', '123456782', 10, per='day', by=6))
        bucket = WidgetRateBucket.objects.get(scope='names')
        self.assertEqual((bucket.count, bucket.bucket_start.hour, bucket.bucket_start.minute), (12, 0, 0))

    def test_the_address_is_the_one_the_platform_wrote_not_one_a_caller_can_send(self):
        from rest_framework.test import APIRequestFactory

        request = APIRequestFactory().post(
            LOOKUP, {}, HTTP_X_FORWARDED_FOR='198.51.100.7', HTTP_X_VERCEL_FORWARDED_FOR='203.0.113.9',
        )
        self.assertEqual(widget_limits.request_ip(request), '203.0.113.9')
        plain = APIRequestFactory().post(LOOKUP, {}, HTTP_X_FORWARDED_FOR='198.51.100.7')
        self.assertEqual(widget_limits.request_ip(plain), '198.51.100.7')

    def test_a_look_up_says_nothing_of_a_family_without_its_phone(self):
        """An identity number and a list of first names used to be enough to learn a family's children."""
        family = TestDataFactory.create_family(parent_id_number='123456782', phone='0501234567')
        child = TestDataFactory.create_child(family=family, first_name='Kid', last_name='Parent', status='active')
        from apps.enrollments.models import LessonEnrollment
        LessonEnrollment.objects.create(child=child, lesson=self.lesson, status='active')
        body = {'parent_id_number': '123456782', 'child_first_name': 'Kid', 'child_last_name': 'Parent'}
        new_family = {
            'family_status': 'new', 'child_status': 'new', 'discount_type': None, 'discount_question': None,
            'enrolled_lesson_ids': [], 'already_registered': False,
        }

        self.assertEqual(self.client.post(LOOKUP, body, format='json').json(), new_family)
        self.assertEqual(
            self.client.post(LOOKUP, {**body, 'parent_phone': '0529999999'}, format='json').json(), new_family,
        )
        known = self.client.post(LOOKUP, {**body, 'parent_phone': '050-1234567'}, format='json').json()
        self.assertEqual(known['child_id'], str(child.id))
        self.assertEqual(known['enrolled_lesson_ids'], [str(self.lesson.id)])

        Family.objects.filter(id=family.id).update(widget_identification_blocked_at='2026-10-02T00:00:00Z')
        self.assertEqual(
            self.client.post(LOOKUP, {**body, 'parent_phone': '0501234567'}, format='json').json(), new_family,
        )

    def test_a_blank_identity_number_does_not_land_on_a_family_without_one(self):
        """A space passed the "required" check and matched the first card with no identity number."""
        stranger = TestDataFactory.create_family(parent_id_number='', phone='0507777777')
        for bad in (' ', 'abc', '123456789', '1234567890'):
            for url in (REGISTER, '/api/v1/customers/widget/trial-register/'):
                response = self.client.post(url, {**self._item(), 'parent_id_number': bad}, format='json')
                self.assertEqual(response.status_code, 400, (url, bad, response.content))
            quote = self.client.post(QUOTE, {'items': [{**self._item(), 'parent_id_number': bad}]}, format='json')
            self.assertEqual(quote.status_code, 400, bad)
        self.assertEqual(stranger.children.count(), 0)
        self.assertEqual(Family.objects.count(), 1)

    def test_the_request_cannot_wave_the_yearly_fee(self):
        response = self.client.post(REGISTER, {**self._item(), 'include_registration_fee': False}, format='json')

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['registration_fee'], 120.0)

    def test_a_quote_is_for_one_parent(self):
        other = {**self._item(), 'parent_id_number': '222222226'}

        response = self.client.post(QUOTE, {'items': [self._item(), other]}, format='json')

        self.assertEqual(response.status_code, 400)

    def test_guessing_a_familys_children_through_quotes_is_bounded(self):
        TestDataFactory.create_family(parent_id_number='123456782', phone='0507777777')
        with patch('apps.customers.widget_views.UNPROVEN_QUOTE_DAILY_ITEMS', 3):
            two = self.client.post(QUOTE, {'items': [self._item(), self._item(child_first_name='B')]}, format='json')
            more = self.client.post(QUOTE, {'items': [self._item(), self._item(child_first_name='C')]}, format='json')

        self.assertEqual(two.status_code, 200, two.content)
        self.assertEqual(more.status_code, 429)

    def test_a_quote_cannot_hide_guesses_behind_one_good_item(self):
        """Every item is looked at: the card's phone on the first must not carry wrong ones behind it."""
        TestDataFactory.create_family(parent_id_number='123456782', phone='0501234567')
        guess = self._item(parent_phone='0529999999', child_first_name='B')
        with patch('apps.customers.widget_views.UNPROVEN_QUOTE_DAILY_ITEMS', 1):
            response = self.client.post(QUOTE, {'items': [self._item(), guess]}, format='json')

        self.assertEqual(response.status_code, 429)

    def test_five_wrong_phones_silence_the_look_up_and_the_quote_for_the_right_one_too(self):
        """Otherwise "known" or "refused" would say which phone is the family's."""
        from apps.customers.identification_models import WidgetIdentifyAttempt

        family = TestDataFactory.create_family(parent_id_number='123456782', phone='0501234567')
        TestDataFactory.create_child(family=family, first_name='Kid', last_name='Parent', status='active')
        body = {'parent_id_number': '123456782', 'child_first_name': 'Kid', 'child_last_name': 'Parent'}
        for last in range(5):
            self.client.post(LOOKUP, {**body, 'parent_phone': f'052999999{last}'}, format='json')
        self.assertEqual(WidgetIdentifyAttempt.objects.filter(outcome='mismatch').count(), 5)

        look = self.client.post(LOOKUP, {**body, 'parent_phone': '0501234567'}, format='json').json()
        quote = self.client.post(QUOTE, {'items': [self._item()]}, format='json')

        self.assertEqual(look['family_status'], 'new')
        self.assertEqual(quote.status_code, 429)
        # Registration itself goes on.
        self.assertEqual(self.client.post(REGISTER, self._item(), format='json').status_code, 201)

    def test_a_look_up_without_a_phone_is_not_a_guess(self):
        """The form as it was sends no phone; it is told nothing and counted as nothing."""
        from apps.customers.identification_models import WidgetIdentifyAttempt

        TestDataFactory.create_family(parent_id_number='123456782', phone='0501234567')
        for _ in range(8):
            self._lookup()

        self.assertEqual(WidgetIdentifyAttempt.objects.count(), 0)

    def test_registrations_from_one_address_stop_at_the_hourly_count_whatever_the_phone(self):
        with patch('apps.customers.widget_views.WIDGET_REGISTER_HOURLY_LIMIT', 1):
            first = self.client.post(REGISTER, self._item(), format='json')
            second = self.client.post(REGISTER, self._item(child_first_name='B', child_id_number='345678903'), format='json')

        self.assertEqual((first.status_code, second.status_code), (201, 429))

    def test_an_identity_number_is_plain_digits_and_not_a_row_of_zeros(self):
        from apps.customers.widget_views import _valid_parent_id

        for good in ('123456782', '000000018', '12344'):
            self.assertTrue(_valid_parent_id(good), good)
        for bad in ('', ' ', '0', '000000000', '²²²²²²²²²', '١٢٣٤٥٦٧٨٢', '1234', '123456789', None, 123):
            self.assertFalse(_valid_parent_id(bad), bad)

    def test_one_household_on_ipv6_is_one_address(self):
        from rest_framework.test import APIRequestFactory

        one = APIRequestFactory().post(LOOKUP, {}, HTTP_X_VERCEL_FORWARDED_FOR='2001:db8:1:2:aaaa::1')
        two = APIRequestFactory().post(LOOKUP, {}, HTTP_X_VERCEL_FORWARDED_FOR='2001:db8:1:2:bbbb::9')
        self.assertEqual(widget_limits.request_ip(one), widget_limits.request_ip(two))

    def test_a_quote_with_the_familys_own_phone_is_never_counted(self):
        TestDataFactory.create_family(parent_id_number='123456782', phone='0501234567')
        with patch('apps.customers.widget_views.UNPROVEN_QUOTE_DAILY_ITEMS', 0):
            response = self.client.post(QUOTE, {'items': [self._item()]}, format='json')

        self.assertEqual(response.status_code, 200, response.content)

    def test_an_unexpected_failure_tells_the_parent_a_code_and_not_the_error(self):
        with patch(
            'apps.customers.widget_views._resolve_family_and_child',
            side_effect=RuntimeError('null value in column "secret_column" of relation "families"'),
        ):
            response = self.client.post(REGISTER, self._item(), format='json')

        self.assertEqual(response.status_code, 500)
        error = response.json()['error']
        self.assertNotIn('secret_column', error)
        self.assertNotIn('families', error)
        self.assertRegex(error, r'קוד [0-9A-F]{6}')
        self.assertEqual(Family.objects.count(), 0)
