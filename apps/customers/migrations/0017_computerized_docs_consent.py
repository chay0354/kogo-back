"""Record each family's consent to receive tax documents by email — סעיף 18ב(ג).

Only the three consent columns are added here. `makemigrations` also offers an
unrelated index rename on cronheartbeat and an alter on payment.registration_fee,
both of which were already outstanding before this change; they are left for the
migration that owns them rather than folded into a bookkeeping-compliance one.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('customers', '0016_payment_trial_credit'),
    ]

    operations = [
        migrations.AddField(
            model_name='family',
            name='computerized_docs_consent_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='הסכמה לקבלת מסמכים ממוחשבים'),
        ),
        migrations.AddField(
            model_name='family',
            name='computerized_docs_consent_revoked_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='ביטול ההסכמה'),
        ),
        migrations.AddField(
            model_name='family',
            name='computerized_docs_consent_source',
            field=models.CharField(blank=True, help_text="היכן ניתנה ההסכמה — הרשמה בווידג'ט, CRM, אתר", max_length=50, null=True, verbose_name='מקור ההסכמה'),
        ),
    ]
