"""Move trial enrollments off blocked holiday dates and write a CSV report."""
import csv
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.enrollments.trial_reminders import reschedule_blocked_trial_enrollments


class Command(BaseCommand):
    help = (
        "Move active trial signups off BLOCKED_TRIAL_LESSON_DATES "
        "(13/9, 20/9, 21/9 by default) to the next date of the same חוג, "
        "and write a CSV of everyone who was moved."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='List who would be moved without writing to the database.',
        )
        parser.add_argument(
            '--output',
            default='',
            help='CSV path for the moved-users list (UTF-8).',
        )

    def handle(self, *args, **options):
        rows = reschedule_blocked_trial_enrollments(dry_run=options['dry_run'])
        moved = [r for r in rows if r.get('moved')]
        skipped = [r for r in rows if not r.get('moved')]

        output = options['output']
        if not output:
            reports = Path(__file__).resolve().parents[4] / 'reports'
            reports.mkdir(parents=True, exist_ok=True)
            output = str(reports / 'moved-trial-lessons-2026-09.csv')

        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            'enrollment_id',
            'child_id',
            'child_first_name',
            'child_last_name',
            'child_status',
            'family_name',
            'parent_name',
            'parent_phone',
            'email',
            'course',
            'branch',
            'old_trial_date',
            'new_trial_date',
            'moved',
        ]
        with path.open('w', encoding='utf-8-sig', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)

        prefix = 'DRY-RUN ' if options['dry_run'] else ''
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}Moved {len(moved)} trial enrollments "
            f"({len(skipped)} could not find a next date). CSV: {path}"
        ))
