import os

from django.core.management.base import BaseCommand

from apps.wahub import demo


class Command(BaseCommand):
    help = (
        'Local only: fifteen invented conversations in which the old bot failed the way the real one did, '
        'the shadow bot\'s answer beside each (stub without an Anthropic key), and the reviewer\'s proposals. '
        'Imports the old bot\'s knowledge first when it is missing. Phones 050-555XXXX. '
        'Refuses any database whose name does not start with kogo_local.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--admin-password', default='', help='Password for the local manager when it has to be created.')
        parser.add_argument('--no-bot', action='store_true', help='Only the conversations; leave the shadow and the reviewer to the cron.')

    def handle(self, *args, **options):
        database = demo.ensure_local_database()
        password = options['admin_password'] or os.environ.get('KOGO_LOCAL_ADMIN_PASSWORD', '')
        user, _created = demo.ensure_local_admin(password)
        made = demo.seed_shadow_demo(user, run_bot=not options['no_bot'])
        self.stdout.write(self.style.SUCCESS(
            f'{database}: {made["contacts"]} שיחות בדויות ({made["messages"]} הודעות), {made["shadow_replies"]} תשובות צל, '
            f'{made["proposals"]} הצעות עדכון ממתינות; רשומות ידע שיובאו עכשיו: {made["knowledge_imported"]}.'
        ))
