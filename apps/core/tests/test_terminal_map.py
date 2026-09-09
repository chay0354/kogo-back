"""
Five terminal settings and no screen that said which was which. These hold the
map honest: a flow points at the setting that really clears it, two settings
holding one value collapse to one terminal, a setting no flow reads is named
as unused, and no key ever leaves the endpoint.
"""
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.core.terminal_map import terminal_map

User = get_user_model()
URL = '/api/v1/core/tranzila/terminals/'

LIVE = dict(
    TRANZILA_TERMINAL='shopiframe',
    TRANZILA_TOKEN_TERMINAL='shopiframe',
    TRANZILA_PROD_TERMINAL='restmain',
    TRANZILA_PROD_TOKEN_TERMINAL='restmain',
    TRANZILA_BILLING_TERMINAL='',
    TRANZILA_PUBLIC_KEY='pk-secret',
    TRANZILA_SECRET_KEY='sk-secret',
)


def make_user(username, role):
    user = User.objects.create_user(username=username, password='pw-for-tests')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return User.objects.get(pk=user.pk)


@override_settings(**LIVE)
class TerminalMapTests(APITestCase):
    def setUp(self):
        self.manager = make_user('manager-terminals@test', UserProfile.ROLE_MANAGER)
        self.worker = make_user('worker-terminals@test', UserProfile.ROLE_WORKER)

    def test_each_flow_names_the_terminal_that_clears_it(self):
        flows = {f['id']: f for f in terminal_map()['flows']}

        self.assertEqual(flows['widget_signup']['terminal'], 'restmain')
        self.assertEqual(flows['recurring']['terminal'], 'restmain')
        self.assertEqual(flows['store_b2c']['terminal'], 'shopiframe')
        self.assertEqual(flows['payment_links']['terminal'], 'shopiframe')
        self.assertEqual(flows['documents']['terminal'], '')
        self.assertFalse(flows['documents']['configured'])

    def test_two_settings_on_one_terminal_collapse_to_one_row(self):
        rows = {r['terminal']: r for r in terminal_map()['terminals']}

        self.assertEqual(set(rows), {'restmain', 'shopiframe'})
        self.assertEqual(
            set(rows['restmain']['settings']),
            {'TRANZILA_PROD_TERMINAL', 'TRANZILA_PROD_TOKEN_TERMINAL'},
        )
        self.assertIn('הוראת קבע חודשית', rows['restmain']['flows'])
        self.assertIn('חנות האתר (B2C)', rows['shopiframe']['flows'])

    def test_a_setting_no_flow_reads_is_reported_unused(self):
        report = terminal_map()

        self.assertIn('TRANZILA_TOKEN_TERMINAL', report['unused_settings'])
        self.assertNotIn('TRANZILA_TERMINAL', report['unused_settings'])

    def test_a_flow_with_no_terminal_is_named(self):
        self.assertIn('הפקת חשבוניות וקבלות', terminal_map()['flows_without_a_terminal'])

    def test_a_placeholder_reads_as_not_configured(self):
        with override_settings(TRANZILA_TERMINAL='mock-terminal'):
            store = next(f for f in terminal_map()['flows'] if f['id'] == 'store_b2c')
            self.assertFalse(store['configured'])

    # --- the endpoint ------------------------------------------------------

    def test_manager_reads_it_and_a_worker_may_not(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(URL)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(len(res.json()['flows']), 7)

        self.client.force_authenticate(self.worker)
        self.assertEqual(self.client.get(URL).status_code, 403)

    def test_no_key_ever_leaves_the_endpoint(self):
        self.client.force_authenticate(self.manager)
        body = self.client.get(URL).content.decode()

        self.assertNotIn('pk-secret', body)
        self.assertNotIn('sk-secret', body)
        self.assertIn('shopiframe', body)
