"""Record each business customer's consent to receive tax documents by email — סעיף 18ב(ג).

The three columns Family got in 0017, on BusinessCustomer (merchants and studio
tenants, who are mailed receipts and credit notes too). Additive and nullable:
the previous code, which inserts customers without them, keeps working while
Vercel's build migrates.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('customers', '0021_child_status_add_inactive'),
    ]

    operations = [
        migrations.AddField(
            model_name='businesscustomer',
            name='computerized_docs_consent_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='הסכמה לקבלת מסמכים ממוחשבים'),
        ),
        migrations.AddField(
            model_name='businesscustomer',
            name='computerized_docs_consent_revoked_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='ביטול ההסכמה'),
        ),
        migrations.AddField(
            model_name='businesscustomer',
            name='computerized_docs_consent_source',
            field=models.CharField(blank=True, help_text='היכן ניתנה ההסכמה — CRM, אתר', max_length=50, null=True, verbose_name='מקור ההסכמה'),
        ),
    ]
