"""
Move every child off a status that no longer exists.

Deliberately a command and not a data migration: deploys run `migrate` on their
own, and rewriting the status of live children is not something a deploy should
do by itself. Run it when you mean to, read the dry run first.

    python manage.py backfill_child_statuses            # report only
    python manage.py backfill_child_statuses --apply    # write
    python manage.py backfill_child_statuses --all      # also re-check the rest

`not_paid` becomes `payment_problem` — it was never set by any code, and the
dashboard already counted the two together. Everything else that is not one of
the statuses (the `expired` / `trial` that calculate_status used to write) is
worked out again from what is recorded about the child.

By default only children on a retired status are touched. `--all` also
re-checks the ones whose status is a real name but no longer matches the
record — a child left on בתהליך רישום whose lessons were all cancelled, say.
That is the wider sweep, so read its dry run carefully before applying it.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.customers.child_status import (
    CHILD_STATUSES,
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
        parser.add_argument(
            '--all', action='store_true', dest='check_all',
            help='Also re-check children whose status is valid but stale.',
        )

    def handle(self, *args, **options):
        apply_changes = options['apply']
        write_history = options['history']
        check_all = options['check_all']

        candidates = (
            Child.objects.all() if check_all
            else Child.objects.exclude(status__in=CHILD_STATUSES)
        )

        moves = Counter()
        reasons = {}
        planned = []
        for child in candidates.select_related('family'):
            mapped = canonical_status(child.status)
            if check_all:
                # Every name is re-checked against the record, real or retired.
                target, why = resolve_child_status(child), 'resolved from the record'
            elif mapped is not None:
                target, why = mapped, 'mapped'
            else:
                target, why = resolve_child_status(child), 'resolved from the record'
            if target == child.status:
                continue
            planned.append((child, child.status, target))
            moves[(child.status, target)] += 1
            reasons[(child.status, target)] = why

        total = len(planned)
        if not total:
            self.stdout.write(self.style.SUCCESS('Every child already holds the status the record supports.'))
            return

        scope = 'whose status no longer matches the record' if check_all else 'on a retired status'
        self.stdout.write(f'{total} child(ren) {scope}:')
        for (was, becomes), count in sorted(moves.items(), key=lambda kv: -kv[1]):
            self.stdout.write(
                f'  {was:<16} → {becomes:<16} {count:>5}   ({reasons[(was, becomes)]})'
            )

        after = Counter(Child.objects.values_list('status', flat=True))
        for _child, was, becomes in planned:
            after[was] -= 1
            after[becomes] += 1
        self.stdout.write('\nHow the list would read afterwards:')
        for status, count in sorted(after.items(), key=lambda kv: -kv[1]):
            if count:
                self.stdout.write(f'  {status:<16} {count:>5}')

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
