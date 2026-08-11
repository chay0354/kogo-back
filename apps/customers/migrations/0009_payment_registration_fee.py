from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('customers', '0008_businesscustomer_branch'),
    ]

    operations = [
        migrations.AddField(
            model_name='payment',
            name='registration_fee',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('0.00'),
                help_text='דמי רישום חד-פעמיים בחיוב ראשון לכל הרשמה לשיעור',
                max_digits=10,
                verbose_name='דמי רישום',
            ),
        ),
    ]
