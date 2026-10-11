import time
from collections import Counter

from django.core.management.base import BaseCommand

from apps.wahub import demo, lead_stage
from apps.wahub.models import LEAD_STAGE_CHOICES, Contact


class Command(BaseCommand):
    help = (
        'Local only: work out the lead stage of every contact that is waiting for a look — what the cron '
        'does a slice at a time — and print how many contacts are in each stage. '
        'Only the lead_* fields are written; nothing is sent. '
        'Refuses any database whose name does not start with kogo_local.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true', help='Look at every contact again, not only the ones waiting.')
        parser.add_argument('--seconds', type=float, default=60, help='Stop after this many seconds (default 60).')

    def handle(self, *args, **options):
        database = demo.ensure_local_database()
        if options['all']:
            # "Never looked at": the scan then takes them all, a batch at a time.
            Contact.objects.update(lead_checked_at=None)
        checked = changed = 0
        deadline = time.monotonic() + max(0.0, options['seconds'])
        while time.monotonic() < deadline and lead_stage.pending_count():
            counts = lead_stage.scan(budget_seconds=min(10.0, deadline - time.monotonic()))
            checked += counts['lead_checked']
            changed += counts['lead_changed']
            if not counts['lead_checked']:
                break       # whatever is left could not be saved now; the next run takes it
        stages = Counter(Contact.objects.values_list('lead_stage', flat=True))
        self.stdout.write(self.style.SUCCESS(
            f'{database}: נבדקו {checked} אנשי קשר, {changed} השתנו; ממתינים לסריקה: {lead_stage.pending_count()}.'
        ))
        for stage, label in LEAD_STAGE_CHOICES:
            self.stdout.write(f'  {stages.get(stage, 0):>5}  {label} ({stage})')
        if stages.get(''):
            self.stdout.write(f'  {stages[""]:>5}  עוד בלי מצב')
