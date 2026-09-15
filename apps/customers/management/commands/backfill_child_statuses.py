"""
Move every child off a status that no longer exists.

Deliberately a command and not a data migration: deploys run `migrate` on their
own, and rewriting the status of live children is not something a deploy should
do by itself. Run it when you mean to, read the dry run first.

    python manage.py backfill_child_statuses            # report only
    python manage.py backfill_child_statuses --apply    # write

`not_paid` becomes `payment_problem` — it was never set by any code, and the
dashboard already counted the two together. Everything else that is not one of
the six (`inactive`, and the `expired` / `trial` that calculate_status used to
write) is worked out again from what is recorded about the child.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.customers.child_status import (
    CHILD_STATUSES,
    LEGACY_STATUS_MAP,
    canonical_status,
    resolve_child_status,
)
from apps.customers.models import Child
from apps.customers.status_history_models import ChildStatusHistory


class Command(BaseCommand):
    help = 'Move children off retired statuses onto the six that exist.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write the changes. Without it nothing is saved.',
        )
        parser.add_argument(
            '--history', action='store_true',
            help='Also record each change in ChildStatusHistory.',
        )

    def handle(self, *args, **options):
        apply_changes = options['apply']
        write_history = options['history']

        stale = Child.objects.exclude(status__in=CHILD_STATUSES)
        total = stale.count()
        if not total:
            self.stdout.write(self.style.SUCCESS('Every child already holds one of the six statuses.'))
            return

        moves = Counter()
        planned = []
        for child in stale.select_related('family'):
            mapped = canonical_status(child.status)
            target = mapped or resolve_child_status(child)
            planned.append((child, child.status, target))
            moves[(child.status, target)] += 1

        self.stdout.write(f'{total} child(ren) on a retired status:')
        for (was, becomes), count in sorted(moves.items(), key=lambda kv: -kv[1]):
            known = 'mapped' if LEGACY_STATUS_MAP.get(was) else 'resolved from the record'
            self.stdout.write(f'  {was:<16} → {becomes:<16} {count:>5}   ({known})')

        if not apply_changes:
            self.stdout.write(self.style.WARNING('\nDry run. Nothing written. Re-run with --apply.'))
            return

        with transaction.atomic():
            for child, was, target in planned:
                child.status = target
                child.save(update_fields=['status', 'updated_at'])
                if write_history:
                    ChildStatusHistory.objects.create(
                        child=child,
                        previous_status=was,
                        new_status=target,
                        reason='התאמה לרשימת הסטטוסים המעודכנת',
                    )

        self.stdout.write(self.style.SUCCESS(f'\n{total} child(ren) updated.'))
