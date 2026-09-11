"""The consent to computerized documents is a paragraph of the terms, added once to what the office wrote."""
import importlib

from django.apps import apps as django_apps
from django.test import TestCase

from apps.core.models import RegistrationTerms
from apps.core.registration_terms_default import DEFAULT_REGISTRATION_TERMS_HTML

migration = importlib.import_module('apps.core.migrations.0020_terms_computerized_docs_clause')


def set_terms(content):
    RegistrationTerms.objects.update_or_create(pk=1, defaults={'content': content})


class TermsClauseMigrationTests(TestCase):
    def run_migration(self):
        migration.add_clause(django_apps, None)
        return RegistrationTerms.objects.get(pk=1).content

    def test_the_paragraph_goes_in_before_the_closing_badge(self):
        set_terms('<p>א</p>\n<p class="terms-badge">ב</p>')
        self.assertEqual(self.run_migration(), f'<p>א</p>\n{migration.CLAUSE}\n<p class="terms-badge">ב</p>')

    def test_terms_without_a_badge_get_it_at_the_end(self):
        set_terms('<p>א</p>')
        self.assertEqual(self.run_migration(), f'<p>א</p>\n{migration.CLAUSE}')

    def test_it_is_added_once(self):
        set_terms('<p>א</p>')
        once = self.run_migration()
        self.assertEqual(self.run_migration(), once)

    def test_terms_the_office_already_worded_are_left_alone(self):
        own = '<p><strong>מסמכים ממוחשבים:</strong> נוסח של המשרד</p>'
        set_terms(own)
        self.assertEqual(self.run_migration(), own)

    def test_no_row_means_nothing_is_created(self):
        RegistrationTerms.objects.filter(pk=1).delete()
        migration.add_clause(django_apps, None)
        self.assertFalse(RegistrationTerms.objects.filter(pk=1).exists())

    def test_the_default_terms_carry_the_same_paragraph(self):
        self.assertIn(migration.CLAUSE, DEFAULT_REGISTRATION_TERMS_HTML)

    def test_undoing_takes_the_paragraph_out_again(self):
        for original in ('<p>א</p>', '<p>א</p>\n<p class="terms-badge">ב</p>'):
            with self.subTest(original=original):
                set_terms(original)
                self.run_migration()
                migration.remove_clause(django_apps, None)
                self.assertEqual(RegistrationTerms.objects.get(pk=1).content, original)
