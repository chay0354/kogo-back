import os

from django.core.management.base import BaseCommand

from apps.wahub import demo


class Command(BaseCommand):
    help = (
        'Local only: fills the "וואטסאפ ולידים" screens with invented conversations '
        '(phones 050-555XXXX) and a few invented families. Refuses any database '
        'whose name does not start with kogo_local.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--admin-password', default='',
            help='Password for the local manager when it has to be created. '
                 'Defaults to the KOGO_LOCAL_ADMIN_PASSWORD environment variable.',
        )

    def handle(self, *args, **options):
        database = demo.ensure_local_database()
        password = options['admin_password'] or os.environ.get('KOGO_LOCAL_ADMIN_PASSWORD', '')
        user, created = demo.ensure_local_admin(password)
        made = demo.seed_demo(user)

        self.stdout.write(self.style.SUCCESS(
            f'{database}: {made["contacts"]} אנשי קשר, {made["messages"]} הודעות, {made["families"]} משפחות בדויות.'
        ))
        if created and password:
            self.stdout.write(f'נוצר מנהל מקומי {demo.LOCAL_ADMIN_EMAIL}. הסיסמה: זו שניתנה לפקודה (ראו README, "כניסה מקומית").')
        elif created:
            self.stdout.write(self.style.WARNING(
                f'נוצר מנהל מקומי {demo.LOCAL_ADMIN_EMAIL} בלי סיסמה. '
                f'קבעו אחת: manage.py changepassword {demo.LOCAL_ADMIN_EMAIL}'
            ))
        else:
            self.stdout.write(f'המנהל המקומי {demo.LOCAL_ADMIN_EMAIL} כבר קיים; הסיסמה שלו לא שונתה.')
