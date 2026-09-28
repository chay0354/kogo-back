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

It holds to the morning fix's protections, because it is the same rule
applied in bulk:

  * a child someone is still charging — a standing order with a card, or a
    cash or cheque plan still running — is never moved to לא פעיל or
    בעיה באשראי (renaming a retired name that already meant that is not a move);
  * `--all --apply` is refused on the 1st–3rd of the month, while the month's
    charges are landing and paid_until_date still shows last month, unless
    `--allow-early-month` says it is meant;
  * each child is re-read under a lock before it is written, so a change the
    office made after the dry run is left alone.
"""
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.customers.child_status import (
    CHILD_STATUSES,
    STATUS_INACTIVE,
    STATUS_PAYMENT_PROBLEM,
    canonical_status,
    resolve_child_status,
    still_charged_child_ids,
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
        parser.add_argument(
            '--allow-early-month', action='store_true',
            help='Let --all --apply run on the 1st-3rd of the month, while paid_until dates lag billing.',
        )

    def handle(self, *args, **options):
        apply_changes = options['apply']
        write_history = options['history']
        check_all = options['check_all']

        # On the first days of a month the monthly charges are still landing,
        # and until each one does its child's paid_until_date shows last month:
        # the rule reads a paying subscriber as someone whose money ran out.
        # The dry run stays open — it writes nothing.
        today = timezone.localdate()
        if check_all and apply_changes and today.day <= 3 and not options['allow_early_month']:
            raise CommandError(
                f'Refusing --all --apply on {today:%d/%m}: on the 1st-3rd of the month paid_until dates '
                'lag billing. Run the dry run, or pass --allow-early-month if this is meant.'
            )

        candidates = (
            Child.objects.all() if check_all
            else Child.objects.exclude(status__in=CHILD_STATUSES)
        )
        still_charged = still_charged_child_ids()

        moves = Counter()
        reasons = {}
        planned = []
        held_back = Counter()
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
            if (
                target in (STATUS_INACTIVE, STATUS_PAYMENT_PROBLEM)
                and mapped != target
                and child.id in still_charged
            ):
                held_back[(child.status, target)] += 1
                continue
            planned.append((child, child.status, target))
            moves[(child.status, target)] += 1
            reasons[(child.status, target)] = why

        if held_back:
            self.stdout.write(
                f'{sum(held_back.values())} child(ren) left as they are — still charged '
                '(a standing order with a card, or a cash / cheque plan):'
            )
            for (was, becomes), count in sorted(held_back.items(), key=lambda kv: -kv[1]):
                self.stdout.write(f'  {was:<16} ↛ {becomes:<16} {count:>5}')
            self.stdout.write('')

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

        written = 0
        with transaction.atomic():
            for child, was, target in planned:
                # Re-read under a lock: the office may have changed it since
                # the list above was drawn up.
                locked = Child.objects.select_for_update().get(pk=child.pk)
                if locked.status != was:
                    continue
                locked.status = target
                # With --history the row below says why; the save signal's own
                # row would be the same change a second time.
                locked._status_history_written = write_history
                locked.save(update_fields=['status', 'updated_at'])
                written += 1
                if write_history:
                    ChildStatusHistory.objects.create(
                        child=locked,
                        previous_status=was,
                        new_status=target,
                        reason='התאמה לרשימת הסטטוסים המעודכנת',
                    )

        self.stdout.write(self.style.SUCCESS(f'\n{written} child(ren) updated.'))
