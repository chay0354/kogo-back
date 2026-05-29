"""Run trial-reminder scheduler manually or from a cron job."""
from django.core.management.base import BaseCommand

from apps.enrollments.trial_reminders import send_due_trial_reminders


class Command(BaseCommand):
    help = (
        "Send WhatsApp trial reminders: test-lesson-10am on trial day at 10:00 Israel, "
        "and after-test 2h after the trial lesson ends."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Identify due reminders without actually firing ManyChat or marking sent.',
        )

    def handle(self, *args, **options):
        summary = send_due_trial_reminders(dry_run=options['dry_run'])
        self.stdout.write(self.style.SUCCESS(f"Trial reminders: {summary}"))
