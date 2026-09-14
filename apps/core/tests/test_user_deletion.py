"""Deleting an internal user: what it refuses, what it warns about, what it keeps."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import LinkedUserAccess, UserProfile
from apps.core.user_deletion import (
    UserDeletionRefused,
    active_manager_count,
    delete_user,
    deletion_preview,
    refusal_reason,
)

User = get_user_model()


def _user(username, role=UserProfile.ROLE_WORKER, active=True, email=None):
    user = User.objects.create_user(
        username=username, email=email if email is not None else username,
        password='pass12345!', is_active=active,
    )
    UserProfile.objects.update_or_create(user=user, defaults={'role': role})
    # Re-fetch: the instance above still carries the profile a signal gave it.
    return User.objects.get(pk=user.pk)


class RefusalTests(TestCase):
    def test_you_cannot_delete_yourself(self):
        me = _user('me@x.com', UserProfile.ROLE_MANAGER)
        _user('other@x.com', UserProfile.ROLE_MANAGER)
        self.assertIn('מחובר', refusal_reason(me, actor=me))

    def test_the_last_active_manager_is_protected(self):
        only = _user('only@x.com', UserProfile.ROLE_MANAGER)
        actor = _user('actor@x.com', UserProfile.ROLE_MANAGER)
        # actor is a manager too, so `only` is not the last one
        self.assertEqual(refusal_reason(only, actor=actor), '')
        actor.is_active = False
        actor.save(update_fields=['is_active'])
        self.assertIn('המנהל הפעיל האחרון', refusal_reason(only, actor=actor))

    def test_a_deactivated_manager_is_not_counted_as_cover(self):
        _user('sleeping@x.com', UserProfile.ROLE_MANAGER, active=False)
        self.assertEqual(active_manager_count(), 0)

    def test_a_worker_is_never_the_last_manager(self):
        worker = _user('w@x.com')
        boss = _user('boss@x.com', UserProfile.ROLE_MANAGER)
        self.assertEqual(refusal_reason(worker, actor=boss), '')

    def test_delete_raises_rather_than_half_doing_it(self):
        me = _user('solo@x.com', UserProfile.ROLE_MANAGER)
        with self.assertRaises(UserDeletionRefused):
            delete_user(me, actor=me)
        self.assertTrue(User.objects.filter(pk=me.pk).exists())


class PreviewTests(TestCase):
    def test_preview_reports_linked_access_both_ways(self):
        owner = _user('owner@x.com', UserProfile.ROLE_MANAGER)
        a = _user('a@x.com')
        b = _user('b@x.com')
        LinkedUserAccess.objects.create(owner=a, linked=b, created_by=owner)
        LinkedUserAccess.objects.create(owner=b, linked=a, created_by=owner)
        preview = deletion_preview(a, actor=owner)
        self.assertEqual(preview['linked_access_granted'], 1)
        self.assertEqual(preview['linked_access_received'], 1)

    def test_preview_says_the_history_survives(self):
        boss = _user('boss2@x.com', UserProfile.ROLE_MANAGER)
        target = _user('t@x.com')
        self.assertIn('נשארים במערכת', deletion_preview(target, actor=boss)['history_note'])

    def test_preview_carries_the_refusal_so_the_screen_can_grey_the_button(self):
        me = _user('me2@x.com', UserProfile.ROLE_MANAGER)
        self.assertTrue(deletion_preview(me, actor=me)['refusal'])


class HistorySurvivesTests(TestCase):
    def test_work_the_user_did_stays_and_only_loses_the_name(self):
        from apps.core.tests.test_fixtures import TestDataFactory
        from apps.customers.status_history_models import ChildStatusHistory

        boss = _user('boss3@x.com', UserProfile.ROLE_MANAGER)
        target = _user('leaving@x.com')
        child = TestDataFactory.create_child()
        row = ChildStatusHistory.objects.create(
            child=child, previous_status='pending', new_status='active',
            changed_by=target, reason='נרשם',
        )

        delete_user(target, actor=boss)

        row.refresh_from_db()
        self.assertIsNone(row.changed_by, 'השם יורד')
        self.assertEqual(row.new_status, 'active', 'הרשומה נשארת')
        self.assertFalse(User.objects.filter(pk=target.pk).exists())


class ApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.manager = _user('mgr@x.com', UserProfile.ROLE_MANAGER)
        self.spare = _user('spare@x.com', UserProfile.ROLE_MANAGER)
        self.target = _user('target@x.com')
        token = Token.objects.create(user=self.manager)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    def test_manager_deletes_a_worker(self):
        res = self.client.delete(f'/api/v1/core/users/{self.target.pk}/')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['deleted'])
        self.assertFalse(User.objects.filter(pk=self.target.pk).exists())

    def test_deleting_yourself_is_refused_with_a_reason(self):
        res = self.client.delete(f'/api/v1/core/users/{self.manager.pk}/')
        self.assertEqual(res.status_code, 400)
        self.assertIn('מחובר', res.data['error'])
        self.assertTrue(User.objects.filter(pk=self.manager.pk).exists())

    def test_preview_endpoint(self):
        res = self.client.get(f'/api/v1/core/users/{self.target.pk}/deletion-preview/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['refusal'], '')
        self.assertIn('history_note', res.data)

    def test_a_worker_cannot_delete_anyone(self):
        worker = _user('grunt@x.com')
        token = Token.objects.create(user=worker)
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        res = c.delete(f'/api/v1/core/users/{self.target.pk}/')
        self.assertEqual(res.status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.target.pk).exists())


class LoginIsGoneTests(TestCase):
    """The point of deleting an account: those details stop working."""

    def setUp(self):
        self.boss = _user('boss4@x.com', UserProfile.ROLE_MANAGER)
        self.target = _user('gone@x.com')

    def test_their_credentials_no_longer_sign_in(self):
        client = APIClient()
        ok = client.post('/api/v1/core/auth/login/',
                         {'email': 'gone@x.com', 'password': 'pass12345!'}, format='json')
        self.assertEqual(ok.status_code, 200, 'לפני המחיקה הם כן נכנסים')

        delete_user(self.target, actor=self.boss)

        after = client.post('/api/v1/core/auth/login/',
                            {'email': 'gone@x.com', 'password': 'pass12345!'}, format='json')
        self.assertNotEqual(after.status_code, 200)

    def test_an_open_session_dies_with_the_account(self):
        token = Token.objects.create(user=self.target)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        self.assertEqual(client.get('/api/v1/core/auth/me/').status_code, 200)

        delete_user(self.target, actor=self.boss)

        self.assertFalse(Token.objects.filter(key=token.key).exists())
        self.assertNotEqual(client.get('/api/v1/core/auth/me/').status_code, 200)

    def test_deactivating_also_blocks_login_without_losing_the_name(self):
        """The reversible alternative the dialog points at."""
        self.target.is_active = False
        self.target.save(update_fields=['is_active'])
        res = APIClient().post('/api/v1/core/auth/login/',
                               {'email': 'gone@x.com', 'password': 'pass12345!'}, format='json')
        self.assertNotEqual(res.status_code, 200)
        self.assertTrue(User.objects.filter(pk=self.target.pk).exists())
