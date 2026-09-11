"""Find charges that never got their חשבונית מס / קבלה, issue them, and check every run.

Every path that takes money issues a document, but each of those calls is
wrapped in try/except and logged as non-fatal, on purpose: a mail or PDF failure
must not undo a charge that already went through. The cost is that a charge can
end up without its document and nobody notices. This finds them.

    python manage.py check_invoices                  # last 90 days, report only
    python manage.py check_invoices --all            # every charge ever made
    python manage.py check_invoices --all --fix      # issue what is missing

A late document is:
  * dated TODAY — the day it is actually produced. Dating it back to the charge
    would put an earlier date on a higher number than documents already issued;
  * issued in the order the money arrived, and says so on its face:
    "הופק באיחור; התשלום התקבל ביום …" (from its activity log, on the PDF);
  * NOT mailed — dozens of old receipts landing at once would read as an error.
    Pass --email only when that is really wanted.

`--backdate` dates late documents on the day the money was received instead.
That is only defensible while nothing later-dated exists in the series, so it
refuses otherwise; agree it with the accountant before using it on real books.

Every run also checks each number series for gaps (נספח ה׳(א)(5)). Issued
documents are never edited or deleted here (סעיף 23(ב)).

What counts as missing, and how a late document is issued, live in
apps/documents/missing_receipts.py — the office's missing-receipts screen reads
the same two, so it never disagrees with this command.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.customers.financial_models import Invoice
from apps.documents.missing_receipts import charge_date, issue_late_receipt, payments_without_invoice
from apps.documents.numbering import SERIES_SUBSCRIPTION
from apps.store.models import StoreInvoice


class Command(BaseCommand):
    help = 'מאתר חיובים שהושלמו ללא חשבונית מס/קבלה, מנפיק אותן, ובודק רצף מספרים'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=90, help='כמה ימים אחורה לבדוק (ברירת מחדל 90)')
        parser.add_argument('--all', action='store_true', help='כל החיובים מאז ומעולם')
        parser.add_argument('--fix', action='store_true', help='להנפיק את המסמכים החסרים')
        parser.add_argument('--email', action='store_true', help='לשלוח את המסמכים שהונפקו גם במייל')
        parser.add_argument('--backdate', action='store_true',
                            help='לתארך מסמך באיחור ביום קבלת הכסף (רק כשאין מסמך מאוחר יותר בסדרה)')

    def handle(self, *args, **options):
        since = None if options['all'] else timezone.now() - timedelta(days=options['days'])
        self.stdout.write('בודק את כל החיובים…\n' if since is None else f'בודק חיובים מ-{since.date().isoformat()} ואילך…\n')

        missing = payments_without_invoice(since)
        self._report_payments(missing)
        if options['fix'] and missing:
            if options['backdate']:
                self._guard_backdate(missing)
            self._issue_missing(missing, send_email=options['email'], backdate=options['backdate'])

        self._report_store(since)
        self._report_gaps()

        if not missing:
            self.stdout.write(self.style.SUCCESS('\nכל חיוב שהושלם בטווח קיבל חשבונית מס/קבלה.'))

    # ------------------------------------------------------------------ payments

    def _report_payments(self, rows):
        if not rows:
            return
        self.stdout.write(self.style.WARNING(f'\n{len(rows)} חיובים ללא חשבונית:\n'))
        for payment in rows:
            child = payment.child.full_name if payment.child_id else '—'
            self.stdout.write(
                f'  {payment.id}  {charge_date(payment).date().isoformat()}  ₪{payment.final_amount}  '
                f'{child}  ({payment.get_payment_type_display()})'
            )

    def _guard_backdate(self, rows):
        """Refuse to put an earlier date on a higher number than an issued document."""
        earliest = charge_date(rows[0])
        later = (
            Invoice.objects
            .filter(invoice_number__startswith=f'{SERIES_SUBSCRIPTION}-', invoice_date__gt=earliest)
            .order_by('invoice_date')
            .first()
        )
        if later is not None:
            raise CommandError(
                f'--backdate סורב: המסמך {later.invoice_number} כבר מתוארך {later.invoice_date.date()} — '
                f'מאוחר מהחיוב הראשון שחסר לו מסמך ({earliest.date()}). הריצו בלי --backdate.'
            )

    def _issue_missing(self, rows, *, send_email: bool, backdate: bool):
        issued = failed = 0
        now = timezone.now()
        for payment in rows:
            charged_on = charge_date(payment)
            try:
                invoice = issue_late_receipt(payment, now=now, send_email=send_email, backdate=backdate)
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f'  נכשל {payment.id}: {exc}'))
                failed += 1
                continue
            if invoice is None:
                # Issued meanwhile — from the office's screen, or a second run.
                self.stdout.write(f'  דולג {payment.id}: כבר יש לו חשבונית')
                continue
            issued += 1
            self.stdout.write(self.style.SUCCESS(
                f'  הונפק {invoice.invoice_number} (תשלום {charged_on.date().isoformat()}) לתשלום {payment.id}'
            ))

        self.stdout.write(f'\nהונפקו {issued} מסמכים' + (f', {failed} נכשלו' if failed else '') + '.')

    # -------------------------------------------------------------------- store

    def _report_store(self, since):
        """A store sale carries its own number, so here only a blank one is a fault."""
        rows = StoreInvoice.objects.filter(payment_status='completed', invoice_number='')
        if since is not None:
            rows = rows.filter(issue_date__gte=since)
        broken = list(rows.order_by('issue_date'))
        if not broken:
            return
        self.stdout.write(self.style.WARNING(f'\n{len(broken)} מכירות חנות ללא מספר מסמך:'))
        for invoice in broken:
            self.stdout.write(f'  {invoice.id}  {invoice.issue_date.date().isoformat()}  ₪{invoice.total_amount}')

    # ------------------------------------------------------------------- series

    def _report_gaps(self):
        """Every number a series handed out must belong to a document that exists."""
        from apps.documents.numbering import continuity

        clean = True
        for run in continuity():
            if run.complete:
                continue
            clean = False
            shown = ', '.join(run.missing[:20])
            more = f' ועוד {len(run.missing) - 20}' if len(run.missing) > 20 else ''
            name = run.name if run.series else f'{run.label} {run.year}'
            self.stdout.write(self.style.ERROR(f'\nחור בסדרה {name}: {shown}{more}'))
        if clean:
            self.stdout.write(self.style.SUCCESS('\nרצף המספרים שלם בכל הסדרות.'))
