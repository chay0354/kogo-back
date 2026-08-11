from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0009_storeproduct_branch_only'),
    ]

    operations = [
        migrations.AddField(
            model_name='storeinvoice',
            name='customer_email',
            field=models.EmailField(blank=True, default='', verbose_name='אימייל לקוח'),
        ),
        migrations.AddField(
            model_name='storeinvoice',
            name='invoice_email_sent_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='חשבונית נשלחה במייל'),
        ),
    ]
