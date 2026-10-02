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

    def _item(self):
        return _payload(course_id=str(self.course.id), lesson_id=str(self.lesson.id))

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

    def test_registration_itself_is_never_stopped_by_the_count(self):
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
