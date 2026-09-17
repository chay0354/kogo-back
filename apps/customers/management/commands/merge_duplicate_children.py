"""
Fold duplicate children and walk-ins whose real child exists.

Dry run by default: prints what would fold into what and what was left for a
person to decide. --apply writes. The daily trial-reminders cron runs the same
thing on its first morning pass, so this is for reading the plan or running it
on demand.
"""
from django.core.management.base import BaseCommand

from apps.customers.child_merge import resolve_duplicates


class Command(BaseCommand):
    help = 'Fold duplicate Child records into the one that stays (dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write the merges.')

    def handle(self, *args, **options):
        result = resolve_duplicates(dry_run=not options['apply'])
        mode = 'APPLIED' if options['apply'] else 'DRY RUN'
        self.stdout.write(
            f"{mode}: planned={result['planned']} merged={result['merged']} "
            f"refused={result['refused']} skipped={result['skipped']} "
            f"(walk-ins={result['ghosts_planned']})"
        )
        for step in result['steps']:
            self.stdout.write(f"  {step['source']} -> {step['target']}  {step['reason']}")
        for item in result['skipped_detail']:
            self.stdout.write(f"  SKIPPED {', '.join(item['children'])}  {item['reason']}")
