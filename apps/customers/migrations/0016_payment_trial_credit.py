from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('customers', '0015_recurring_override_store_amount'),
    ]

    operations = [
        migrations.AddField(
            model_name='payment',
            name='trial_credit_amount',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('0.00'),
                help_text='סכום שקוזז מהחיוב הראשון בזכות שיעור ניסיון בתשלום ששולם קודם.',
                max_digits=10,
                verbose_name='קיזוז שיעור ניסיון',
            ),
        ),
        migrations.AddField(
            model_name='payment',
            name='trial_credit_source',
            field=models.ForeignKey(
                blank=True,
                help_text='התשלום של שיעור הניסיון שנוצל לקיזוז — כל ניסיון מזכה פעם אחת.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='trial_credit_uses',
                to='customers.payment',
                verbose_name='תשלום הניסיון שקוזז',
            ),
        ),
    ]
