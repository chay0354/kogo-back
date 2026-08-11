from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('documents', '0002_alter_documentcounter_unique_together_and_more'),
        ('store', '0010_storeinvoice_customer_email'),
    ]

    operations = [
        migrations.AddField(
            model_name='storeinvoice',
            name='formal_document',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='store_invoices',
                to='documents.formaldocument',
                verbose_name='מסמך טרנזילה',
            ),
        ),
    ]
