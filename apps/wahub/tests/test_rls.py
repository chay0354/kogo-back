"""
The tables hold phones and conversations, and in production the database is
also reachable with Supabase's public key (docs/11-SECURITY-FINDING-ANON-EXPOSURE.md).
Row level security is on for every table of the app, with no policy: nobody but
the owner reads a row — and the application, which is the owner, works as before.
"""
from django.db import connection, transaction
from django.db.utils import DatabaseError

from apps.wahub.models import Contact, ContactEvent, Message, QuickReply, Tag
from apps.wahub.tests.base import WahubTestCase

TABLES = {
    'wahub_tags', 'wahub_quick_replies', 'wahub_contacts', 'wahub_contact_tags',
    'wahub_messages', 'wahub_contact_events',
    # stage 2 (0002): the bot's knowledge, the shadow replies, the review
    'wahub_knowledge_items', 'wahub_knowledge_history', 'wahub_shadow_replies',
    'wahub_trial_questions', 'wahub_knowledge_proposals', 'wahub_service_notes',
}


class RowLevelSecurityTests(WahubTestCase):
    def test_every_table_of_the_app_is_covered(self):
        from django.apps import apps

        models = apps.get_app_config('wahub').get_models(include_auto_created=True)
        self.assertEqual({model._meta.db_table for model in models}, TABLES)

    def test_row_level_security_is_on_and_not_forced_on_the_owner(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relkind = 'r' AND relname LIKE 'wahub\\_%%'"
            )
            rows = {name: (enabled, forced) for name, enabled, forced in cursor.fetchall()}
        self.assertEqual(set(rows), TABLES)
        for table, (enabled, forced) in rows.items():
            with self.subTest(table=table):
                self.assertTrue(enabled)
                self.assertFalse(forced)

    def test_no_policy_lets_anybody_in(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_policies WHERE tablename LIKE 'wahub\\_%%'")
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_the_application_still_reads_and_writes_every_table(self):
        self.incoming('שלום', name='רותם')
        contact = self.contact()
        tag = Tag.objects.create(name='חם')
        contact.tags.add(tag)
        QuickReply.objects.create(title='פתיחה', text='היי')
        self.assertEqual(Contact.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(ContactEvent.objects.count(), 1)
        self.assertEqual(list(contact.tags.values_list('name', flat=True)), ['חם'])
        self.assertEqual(QuickReply.objects.count(), 1)

        Contact.objects.filter(pk=contact.pk).update(name='רותם לוי')
        self.assertEqual(self.get(f'contacts/{contact.id}/').data['name'], 'רותם לוי')
        contact.delete()
        self.assertEqual(Message.objects.count(), 0)

    def test_another_role_sees_no_rows_even_when_granted_the_table(self):
        """What the public key's role would get: the table is there, and it is empty to it."""
        self.incoming('שלום')
        self.assertEqual(Contact.objects.count(), 1)
        with connection.cursor() as cursor:
            try:
                with transaction.atomic():
                    cursor.execute('CREATE ROLE wahub_rls_probe NOLOGIN')
            except DatabaseError:
                self.skipTest('this database user cannot create a role to test with')
            for table in sorted(TABLES):
                cursor.execute(f'GRANT SELECT, INSERT ON "{table}" TO wahub_rls_probe')
            cursor.execute('SET LOCAL ROLE wahub_rls_probe')
            try:
                for table in sorted(TABLES):
                    cursor.execute(f'SELECT count(*) FROM "{table}"')
                    self.assertEqual(cursor.fetchone()[0], 0, table)
                with self.assertRaises(DatabaseError):
                    with transaction.atomic():
                        cursor.execute("INSERT INTO wahub_tags (name, color, created_at) VALUES ('x', '#000000', now())")
            finally:
                cursor.execute('RESET ROLE')
        self.assertEqual(Contact.objects.count(), 1)
