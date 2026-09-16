"""
Every child, the status they hold, the status the record supports, and why.

Read-only. It writes no row and changes nothing — the point is to be able to go
through the customers one by one and check the answer rather than trust it.

    python manage.py audit_child_statuses                    # summary to screen
    python manage.py audit_child_statuses --csv out.csv      # one row per child
    python manage.py audit_child_statuses --only-mismatched  # just the arguments

The CSV carries the evidence beside the verdict — paid up to, completed
payments, live lessons, cancelled lessons, trial ahead, trial held — so a
disagreement can be settled by looking, not by re-running anything.
"""
import csv
from collections import Counter, defaultdict
from datetime import date

from django.core.management.base import BaseCommand
from django.db.models import Prefetch

from apps.customers.child_status import (
    LIVE_ENROLLMENT_STATUSES,
    canonical_status,
    resolve_child_status,
    status_label,
)
from apps.customers.models import Child
from apps.enrollments.models import LessonEnrollment

COLUMNS = [
    'שם', 'סניף', 'סטטוס נוכחי', 'סטטוס לפי הרישום', 'תואם',
    'משולם עד', 'תשלומים שהושלמו', 'שיעורים פעילים', 'שיעורים שבוטלו',
    'ניסיון עתידי', 'ניסיון שהתקיים', 'נוצר', 'מזהה',
]


class Command(BaseCommand):
    help = 'Check every child against the status rules. Writes nothing.'

    def add_arguments(self, parser):
        parser.add_argument('--csv', dest='csv_path', help='Write one row per child here.')
        parser.add_argument(
            '--only-mismatched', action='store_true',
            help='Limit the CSV to children whose status does not match.',
        )
        parser.add_argument('--branch', help='Limit to one branch, by name.')

    def handle(self, *args, **options):
        today = date.today()
        children = (
            Child.objects
            .select_related('family', 'family__branch')
            .prefetch_related(
                Prefetch('lesson_enrollments', queryset=LessonEnrollment.objects.all()),
                'payments',
            )
            .order_by('family__branch__name', 'last_name', 'first_name')
        )
        if options['branch']:
            children = children.filter(family__branch__name=options['branch'])

        rows = []
        stored_counts = Counter()
        resolved_counts = Counter()
        moves = Counter()
        by_branch = defaultdict(Counter)

        for child in children:
            enrollments = list(child.lesson_enrollments.all())
            live = [e for e in enrollments if e.status in LIVE_ENROLLMENT_STATUSES]
            cancelled = [e for e in enrollments if e.status not in LIVE_ENROLLMENT_STATUSES]
            trial_ahead = [e for e in enrollments if e.trial_lesson_date and e.trial_lesson_date >= today]
            trial_held = [e for e in enrollments if e.trial_held_on and e.trial_held_on < today]
            paid_payments = [p for p in child.payments.all() if p.status == 'completed']

            current = child.status
            expected = resolve_child_status(child)
            agrees = current == expected
            branch = child.family.branch.name if child.family and child.family.branch else '—'

            stored_counts[current] += 1
            resolved_counts[expected] += 1
            by_branch[branch]['total'] += 1
            if not agrees:
                moves[(current, expected)] += 1
                by_branch[branch]['mismatched'] += 1

            rows.append({
                'שם': child.full_name,
                'סניף': branch,
                'סטטוס נוכחי': status_label(canonical_status(current) or current),
                'סטטוס לפי הרישום': status_label(expected),
                'תואם': 'כן' if agrees else 'לא',
                'משולם עד': child.paid_until_date.isoformat() if child.paid_until_date else '',
                'תשלומים שהושלמו': len(paid_payments),
                'שיעורים פעילים': len(live),
                'שיעורים שבוטלו': len(cancelled),
                'ניסיון עתידי': trial_ahead[0].trial_lesson_date.isoformat() if trial_ahead else '',
                'ניסיון שהתקיים': trial_held[0].trial_held_on.isoformat() if trial_held else '',
                'נוצר': child.created_at.date().isoformat() if child.created_at else '',
                'מזהה': str(child.id),
                '_agrees': agrees,
            })

        total = len(rows)
        mismatched = sum(moves.values())

        self.stdout.write(f'{total} children checked, {mismatched} hold a status the record does not support.\n')

        self.stdout.write('Held today vs what the record supports:')
        every = sorted(set(stored_counts) | set(resolved_counts))
        self.stdout.write(f'  {"status":<18}{"held":>8}{"supported":>12}')
        for status in every:
            self.stdout.write(f'  {status:<18}{stored_counts[status]:>8}{resolved_counts[status]:>12}')

        if moves:
            self.stdout.write('\nWhere they disagree:')
            for (was, becomes), count in moves.most_common():
                self.stdout.write(f'  {was:<18} → {becomes:<18} {count:>6}')

        if by_branch:
            self.stdout.write('\nBy branch:')
            self.stdout.write(f'  {"branch":<28}{"children":>10}{"mismatched":>12}')
            for branch, counts in sorted(by_branch.items(), key=lambda kv: -kv[1]['mismatched']):
                self.stdout.write(f'  {branch:<28}{counts["total"]:>10}{counts["mismatched"]:>12}')

        if options['csv_path']:
            wanted = [r for r in rows if not r['_agrees']] if options['only_mismatched'] else rows
            with open(options['csv_path'], 'w', newline='', encoding='utf-8-sig') as handle:
                writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(wanted)
            self.stdout.write(self.style.SUCCESS(
                f'\n{len(wanted)} row(s) written to {options["csv_path"]}'
            ))
