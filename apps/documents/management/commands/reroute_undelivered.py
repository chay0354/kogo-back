"""
Decide again where the originals left 'none' should go — by the rules of 25.9.2026.

Until then an original issued without a mail channel (a hand-issued invoice, a
till sale, a late lesson receipt), or for a customer with no address, was
marked 'none' and never reached its customer. Now every original is mailed when
18ב(ד) allows it, put on the hand-delivery list when it does not (or when there
is no address), or held with a reason (signing/service._decide). This command
applies that to the rows written before: each gets its kind's channel and a new
decision. It mails nothing itself — the sign-pending cron mails the rows that
became 'email', a batch per run.

Dry run by default: it prints what would change, counts only (no names, no
addresses). --apply writes the decisions. Archive copies are never touched.

Running it with --apply against production is a production write and, through
the cron, a real mailing to customers: only with the owner's explicit approval.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from apps.documents.signing.service import EMAIL, HELD, PAPER, reroute_undelivered


class Command(BaseCommand):
    help = "מחליט מחדש לאן הולך כל מקור שסומן 'none' (ברירת מחדל: הרצה יבשה, ספירות בלבד)."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='לכתוב את ההחלטות (בלי זה — הרצה יבשה שרק סופרת)')
        parser.add_argument('--dry-run', action='store_true',
                            help='הרצה יבשה (ברירת המחדל; קיים לבהירות)')
        parser.add_argument('--since', default='',
                            help='רק מסמכים מתאריך זה ואילך (YYYY-MM-DD)')

    def handle(self, *args, **options):
        if options['apply'] and options['dry_run']:
            raise CommandError('--apply ו־--dry-run יחד — יש לבחור אחד')
        since = None
        if options['since']:
            since = parse_date(options['since'])
            if since is None:
                raise CommandError('--since חייב להיות תאריך בפורמט YYYY-MM-DD')
        apply = bool(options['apply'])

        counts = reroute_undelivered(apply=apply, since=since)

        mode = 'נכתב' if apply else 'הרצה יבשה — לא נכתב דבר'
        self.stdout.write(f'{mode}. נבדקו {counts["examined"]} מקורות שסומנו none.')
        self.stdout.write(f'  במייל (ה-cron ישלח): {counts.get(EMAIL, 0)}')
        self.stdout.write(f'  למסירה ידנית: {counts.get(PAPER, 0)}')
        self.stdout.write(f'  מוחזקים עם סיבה: {counts.get(HELD, 0)}')
        if counts['errors']:
            self.stdout.write(self.style.WARNING(f'  לא הוחלט (שגיאה, ראו בלוג): {counts["errors"]}'))
        for kind, per in sorted(counts['by_kind'].items()):
            parts = ', '.join(f'{delivery}: {n}' for delivery, n in sorted(per.items()))
            self.stdout.write(f'  {kind}: {parts}')
        if not apply and counts['examined']:
            self.stdout.write('להחלה: --apply (בפרודקשן — רק באישור הבעלים; ה-cron ישלח את מה שיעבור למייל).')
