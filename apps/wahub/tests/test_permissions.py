"""Managers only. A partner, an instructor and a stranger get nothing — not a list, not a count, not a key."""
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.wahub.models import Contact, Tag
from apps.wahub.tests.base import BASE, WahubTestCase, client_for, make_user


class PermissionTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.incoming('שלום')
        self.contact_id = self.contact().id
        self.tag = Tag.objects.create(name='חם')
        self.partner = client_for(make_user('partner@wahub.test', UserProfile.ROLE_PARTNER))
        self.instructor = client_for(make_user('worker@wahub.test', UserProfile.ROLE_WORKER))
        self.anonymous = APIClient()

    def calls(self):
        c = self.contact_id
        return [
            ('get', 'contacts/', None),
            ('post', 'contacts/', {'phone': '0505550199', 'name': 'x'}),
            ('get', 'contacts/counts/', None),
            ('get', 'contacts/updates/', None),
            ('get', 'summary/', None),
            ('get', 'status/', None),
            ('post', 'settings/inbound-key/', {}),
            ('get', f'contacts/{c}/', None),
            ('patch', f'contacts/{c}/', {'name': 'x'}),
            ('get', f'contacts/{c}/messages/', None),
            ('post', f'contacts/{c}/read/', {}),
            ('post', f'contacts/{c}/send/', {'text': 'שלום'}),
            ('post', f'contacts/{c}/send-flow/', {'automation_id': 'content1'}),
            ('post', f'contacts/{c}/takeover/', {}),
            ('post', f'contacts/{c}/release/', {}),
            ('post', f'contacts/{c}/needs-human/', {'needs_human': True}),
            ('patch', f'contacts/{c}/followup/', {'status': 'answered'}),
            ('put', f'contacts/{c}/tags/', {'tag_ids': [self.tag.id]}),
            ('post', f'contacts/{c}/recheck/', {}),
            ('post', f'contacts/{c}/analyze/', {}),
            ('get', 'tags/', None),
            ('post', 'tags/', {'name': 'חדש'}),
            ('patch', f'tags/{self.tag.id}/', {'name': 'אחר'}),
            ('delete', f'tags/{self.tag.id}/', None),
            ('get', 'quick-replies/', None),
            ('post', 'quick-replies/', {'title': 'x', 'text': 'y'}),
            ('get', 'leads/unregistered/', None),
            ('get', 'for-customer/?family=00000000-0000-0000-0000-000000000000', None),
            ('post', 'for-customer/recheck/', {'family': '00000000-0000-0000-0000-000000000000'}),
        ]

    def _call(self, client, method, path, data):
        return getattr(client, method)(f'{BASE}/{path}', data, format='json') if data is not None \
            else getattr(client, method)(f'{BASE}/{path}')

    def test_a_partner_is_refused_everywhere(self):
        for method, path, data in self.calls():
            with self.subTest(call=f'{method} {path}'):
                self.assertEqual(self._call(self.partner, method, path, data).status_code, 403)

    def test_an_instructor_is_refused_everywhere(self):
        for method, path, data in self.calls():
            with self.subTest(call=f'{method} {path}'):
                self.assertEqual(self._call(self.instructor, method, path, data).status_code, 403)

    def test_a_stranger_is_refused_everywhere(self):
        for method, path, data in self.calls():
            with self.subTest(call=f'{method} {path}'):
                self.assertIn(self._call(self.anonymous, method, path, data).status_code, (401, 403))

    def test_nothing_was_changed_by_the_refused_calls(self):
        for client in (self.partner, self.instructor, self.anonymous):
            for method, path, data in self.calls():
                self._call(client, method, path, data)
        contact = Contact.objects.get(pk=self.contact_id)
        self.assertEqual(Contact.objects.count(), 1)
        self.assertEqual((contact.followup_status, contact.handled_by, contact.unread_count), ('', 'bot', 1))
        self.assertEqual(Tag.objects.get().name, 'חם')
        self.assertEqual(contact.messages.count(), 1)

    def test_a_user_with_no_profile_is_refused(self):
        from django.contrib.auth import get_user_model

        bare = get_user_model().objects.create_user(username='bare@wahub.test', email='bare@wahub.test', password='x12345678!')
        UserProfile.objects.filter(user=bare).delete()
        self.assertEqual(client_for(bare).get(f'{BASE}/contacts/').status_code, 403)

    def test_the_manager_is_let_in(self):
        self.assertEqual(self.get('contacts/').status_code, 200)

    def test_the_two_keys_are_on_the_list_a_manager_may_store(self):
        from apps.core.integration_views import IntegrationCredentialView

        self.assertIn('WAHUB_INBOUND_KEY', IntegrationCredentialView.ALLOWED_KEYS)
        self.assertIn('ANTHROPIC_API_KEY', IntegrationCredentialView.ALLOWED_KEYS)
        listed = self.client.get('/api/v1/core/auth/integration-credentials/')
        self.assertEqual(listed.status_code, 200)
        keys = {row['key'] for row in listed.data['credentials']}
        self.assertTrue({'WAHUB_INBOUND_KEY', 'ANTHROPIC_API_KEY', 'SUPABASE_SERVICE_ROLE_KEY'} <= keys)
        # The value itself is never handed back.
        self.post('settings/inbound-key/')
        again = self.client.get('/api/v1/core/auth/integration-credentials/')
        self.assertNotIn('sha256', str(again.data))
