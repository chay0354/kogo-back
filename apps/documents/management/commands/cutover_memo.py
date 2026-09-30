"""
The cutover memo for the accountant to sign (apps/documents/cutover_memo.py).

    python manage.py cutover_memo --switch-at "2026-09-24 09:57" --old-software "שם התוכנה" \\
        [--old-last combined=121882@2026-09-23 --old-last receipt=40413@2026-09-22 ...] \\
        [--out cutover-memo.pdf] [--sign]

--switch-at is Israel time; left out, the memo prints a blank line for it.
--old-last gives the previous program's last number of a type (tax_invoice,
combined, receipt, transaction_invoice, credit_invoice) when its documents were
not imported; imported documents (LegacyDocument) win, and a typed value that
disagrees with them is printed beside them to be checked. A type with neither
gets a blank line to fill in by hand.

--sign signs the PDF with the configured backend — in production the Cloud KMS
key; locally only the test key. Reads the database, writes nothing to it, and
sends nothing.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.documents.cutover_memo import MemoInputError, cutover_memo_pdf, parse_old_last, parse_switch_at


class Command(BaseCommand):
    help = 'Draw the cutover memo (old software -> Kogo) as a PDF for the owner and the accountant to sign.'

    def add_arguments(self, parser):
        parser.add_argument('--switch-at', help='When Kogo took over, Israel time: "YYYY-MM-DD HH:MM".')
        parser.add_argument('--old-software', default='', help="The previous program's name.")
        parser.add_argument('--old-last', action='append', default=[],
                            help='TYPE=NUMBER[@YYYY-MM-DD], repeatable: the previous program\'s last number of a type.')
        parser.add_argument('--out', help='Where to write the PDF. Default: cutover-memo-<today>.pdf')
        parser.add_argument('--sign', action='store_true', help='Sign the PDF with the configured signing backend.')

    def handle(self, *args, **options):
        from apps.documents.signing import SigningUnavailable

        try:
            switch_at = parse_switch_at(options.get('switch_at'))
            manual = parse_old_last(options.get('old_last'))
        except MemoInputError as exc:
            raise CommandError(str(exc)) from exc
        try:
            pdf = cutover_memo_pdf(switch_at=switch_at, old_software=options.get('old_software') or '',
                                   manual_old_last=manual, sign=options.get('sign'))
        except SigningUnavailable as exc:
            raise CommandError(f'The memo could not be signed: {exc}') from exc

        out = options.get('out') or f'cutover-memo-{timezone.localdate():%Y%m%d}.pdf'
        with open(out, 'wb') as handle:
            handle.write(pdf)
        signed = ' (signed)' if options.get('sign') else ''
        self.stdout.write(self.style.SUCCESS(f'Cutover memo{signed}: {out} ({len(pdf)} bytes)'))
