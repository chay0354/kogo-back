"""
Eight store sales of September 2026 that never took any money: the website and
the till's hosted page paid on Tranzila's test terminal (realtest) until 23.9,
so the customer's card was never charged (docs/15-STORE-CHAIN.md, finding Q).
The owner, 14.10.2026: "תעיף את המכירות חנות שלא קרו כי זה לא קרה".

Each is in the old numbering (INV-…), which is no fiscal document, so it is
corrected by its status: completed → failed. The reports then leave it out —
the income with no document, the register, the dashboard's store revenue. Its
stock is left as it is, on the owner's word ("לא לגעת במלאי").

Guarded: a row changes only if it is still completed and still holds the same
test-terminal transaction number found in the check of 14.10. Reversible.
The ST-numbered sales of the same kind (ST-2026-000001/3/5) are documents and
are not touched here.
"""
from django.db import migrations

# invoice number → the realtest transaction number it holds (checked 14.10.2026:
# none of these exists on fxpmichalweb, fxpmichalwebtok, cogolive or cogolivetok).
SALES = {
    'INV-202609-00001': '25300',
    'INV-202609-00002': '25422',
    'INV-202609-00006': '25539',
    'INV-202609-00007': '25562',
    'INV-202609-00008': '25597',
    'INV-202609-00009': '25634',
    'INV-202609-00021': '26355',
    'INV-202609-00022': '26385',
}
NOTE = 'בוטל 14.10.2026: שולם במסוף הבדיקות realtest — לא נגבה כסף, העסקה לא התקיימה.'


def void(apps, schema_editor):
    StoreInvoice = apps.get_model('store', 'StoreInvoice')
    for number, index in SALES.items():
        row = StoreInvoice.objects.filter(
            invoice_number=number, tranzila_transaction_id=index, payment_status='completed',
        ).first()
        if row is None:
            continue
        notes = f'{row.notes}\n{NOTE}'.strip() if row.notes else NOTE
        # update(), not save(): nothing here may reach the website or a mailbox.
        StoreInvoice.objects.filter(pk=row.pk).update(payment_status='failed', notes=notes)


def restore(apps, schema_editor):
    StoreInvoice = apps.get_model('store', 'StoreInvoice')
    for row in StoreInvoice.objects.filter(invoice_number__in=list(SALES), payment_status='failed',
                                           notes__contains=NOTE):
        notes = row.notes.replace(f'\n{NOTE}', '').replace(NOTE, '').strip()
        StoreInvoice.objects.filter(pk=row.pk).update(payment_status='completed', notes=notes)


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0016_store_invoice_payment_followup'),
    ]

    operations = [
        migrations.RunPython(void, restore),
    ]
