"""
The cash plans registered under the original design (CashPlan.mode NULL), for the accountant (E).

Until 30.9.2026 a cash plan issued a receipt for the whole sum when the cash
was taken, and then a document on the 1st of each month — by default a
חשבונית מס/קבלה, which recorded the same cash a second time. From WS-3 a new
plan gets one חשבונית מס/קבלה for the whole sum (D1), and an older plan's
remaining months get a tax invoice settled against its receipt instead.

This lists every older plan: its receipt, how many monthly invoice-receipts it
already issued and the cash they recorded again, and the months still to
come. Read-only: nothing is changed, and an issued document is never edited
(סעיף 23(ב)); what to do about each is the accountant's call.

It prints numbers and amounts only — no customer's name or details.

    python manage.py report_older_cash_plans
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.documents.models import CashPlan


class Command(BaseCommand):
    help = 'List the cash plans of the original design and the cash their monthly invoice-receipts recorded again (read-only).'

    def handle(self, *args, **options):
        plans = (
            CashPlan.objects.filter(mode__isnull=True)
            .select_related('receipt')
            .prefetch_related('months', 'months__document')
            .order_by('created_at')
        )
        self.stdout.write('plan_id,status,receipt,receipt_total,monthly_irms,cash_recorded_again,'
                          'monthly_tax_invoices,months_to_come,to_come_amount')
        doubled_total = Decimal('0')
        count = 0
        for plan in plans:
            count += 1
            months = list(plan.months.all())
            irms = [m for m in months if m.document_id and m.document.document_type == 'combined']
            invoices = [m for m in months if m.document_id and m.document.document_type == 'tax_invoice']
            to_come = [m for m in months if m.status == 'pending']
            doubled = sum((m.document.total_amount for m in irms), Decimal('0'))
            doubled_total += doubled
            receipt = plan.receipt
            self.stdout.write(','.join([
                str(plan.pk), plan.status,
                receipt.document_number if receipt else '',
                f'{receipt.total_amount:.2f}' if receipt else '',
                str(len(irms)), f'{doubled:.2f}', str(len(invoices)),
                str(len(to_come)), f"{sum((m.amount for m in to_come), Decimal('0')):.2f}",
            ]))
        self.stdout.write(f'Older cash plans: {count}')
        self.stdout.write(f'Cash recorded again by monthly invoice-receipts, in all: {doubled_total:.2f}')
