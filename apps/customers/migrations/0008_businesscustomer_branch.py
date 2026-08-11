from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_branch_is_external'),
        ('customers', '0007_recurringpayment_pending_amount'),
    ]

    operations = [
        migrations.AddField(
            model_name='businesscustomer',
            name='branch',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='business_customers',
                to='core.branch',
                verbose_name='שיוך לסניף',
            ),
        ),
    ]
