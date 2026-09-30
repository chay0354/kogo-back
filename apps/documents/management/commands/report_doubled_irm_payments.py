"""
The invoice-receipts whose payment rows add up to more than the document (G).

Until 25.9.2026 a חשבונית מס/קבלה issued by hand wrote one payment row per
method chosen, each for the whole total: paid half in cash and half by check,
it recorded twice its money — and the uniform file (D120) and every report
that sums payment rows carried the double. This lists the documents it
happened to, for the accountant. Read-only: nothing is changed, and an issued
document is never edited (סעיף 23(ב)); what to do about each is the
accountant's call.

It prints numbers and amounts only — no customer's name or details.

    python manage.py report_doubled_irm_payments [--year 2026]
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Count, DecimalField, Sum, Value
from django.db.models.functions import Coalesce

from apps.documents.models import FormalDocument


class Command(BaseCommand):
    help = 'List hand-issued invoice-receipts whose payment rows add up to more than their total (read-only).'

    def add_arguments(self, parser):
        parser.add_argument('--year', type=int, default=None, help='Only documents dated in this year.')

    def handle(self, *args, year=None, **options):
        documents = FormalDocument.objects.filter(document_type='combined')
        if year is not None:
            documents = documents.filter(document_date__year=year)
        rows = (
            documents
            .annotate(
                rows=Count('payments'),
                paid=Coalesce(Sum('payments__amount'), Value(Decimal('0')), output_field=DecimalField()),
            )
            .order_by('document_date', 'document_number')
            .values_list('document_number', 'document_date', 'total_amount', 'paid', 'rows')
        )
        checked = 0
        doubled = []
        for number, day, total, paid, count in rows:
            checked += 1
            if paid > total:
                doubled.append((number, day, total, paid, count))

        scope = f' dated in {year}' if year is not None else ''
        self.stdout.write(f'Invoice-receipts (combined) checked{scope}: {checked}')
        self.stdout.write(f'Payment rows above the document total: {len(doubled)}')
        if not doubled:
            return
        self.stdout.write('number,document_date,total,payment_rows_sum,payment_rows,excess')
        excess_total = Decimal('0')
        for number, day, total, paid, count in doubled:
            excess = paid - total
            excess_total += excess
            self.stdout.write(f'{number},{day.isoformat()},{total:.2f},{paid:.2f},{count},{excess:.2f}')
        self.stdout.write(f'Excess recorded in payment rows, in all: {excess_total:.2f}')
