from django.test import TestCase, override_settings
from rest_framework.test import APIClient


@override_settings(CRON_TOKEN='test-cron-token')
class CronBillingStatusTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_status_requires_token(self):
        res = self.client.get('/api/v1/customers/cron/recurring-billing/status/')
        self.assertEqual(res.status_code, 401)

    def test_status_reports_no_vercel_hit_yet(self):
        res = self.client.get(
            '/api/v1/customers/cron/recurring-billing/status/',
            HTTP_X_CRON_TOKEN='test-cron-token',
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body['ok'])
        self.assertFalse(body['vercel_cron_seen'])
        self.assertIsNone(body['last_vercel_cron'])
        self.assertEqual(body['due_today'], 0)
