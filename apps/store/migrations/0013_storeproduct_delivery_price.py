from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0012_drop_orphan_tranzila_invoice_columns'),
    ]

    operations = [
        migrations.AddField(
            model_name='storeproduct',
            name='delivery_price',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('0.00'),
                help_text='Per-unit shipping fee charged on delivery/online sales (0 = none)',
                max_digits=10,
                validators=[MinValueValidator(Decimal('0.00'))],
                verbose_name='מחיר משלוח',
            ),
        ),
    ]
