"""Run trial-reminder scheduler manually or from a cron job."""
from django.core.management.base import BaseCommand

from apps.enrollments.trial_reminders import send_due_trial_reminders


class Command(BaseCommand):
    help = (
        "Send WhatsApp 'reminder' automations for trial enrollments: "
        "same registration evening (7pm Israel) and 72h after the trial lesson."
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
