# Generated manually — links CRM store products to the B2C website catalog.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0007_storeinvoice_amount_paid_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='storeproduct',
            name='website_legacy_id',
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                unique=True,
                verbose_name='מזהה מוצר באתר',
                help_text='Legacy product id on the public B2C website (links stock sync)',
            ),
        ),
        migrations.AddField(
            model_name='storeinvoice',
            name='website_order_number',
            field=models.CharField(
                blank=True,
                max_length=50,
                null=True,
                unique=True,
                verbose_name='מספר הזמנה מהאתר',
            ),
        ),
        migrations.AddField(
            model_name='storeinvoice',
            name='website_idempotency_key',
            field=models.CharField(
                blank=True,
                max_length=64,
                null=True,
                unique=True,
                verbose_name='מפתח כפילות מהאתר',
            ),
        ),
    ]
