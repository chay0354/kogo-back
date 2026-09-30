from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('customers', '0024_course_checkout'),
        ('documents', '0015_draft_settlements'),
        ('payment_links', '0005_tranzila_terminal'),
    ]

    operations = [
        migrations.AddField(
            model_name='paymentlink',
            name='kind',
            field=models.CharField(
                choices=[('general', 'קישור תשלום כללי'), ('business_charge', 'גבייה עסקית חד־פעמית')],
                default='general', max_length=24,
            ),
        ),
        migrations.AddField(
            model_name='paymentlink',
            name='business_customer',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name='one_time_charge_links', to='customers.businesscustomer', verbose_name='לקוח עסקי',
            ),
        ),
        migrations.AddField(
            model_name='paymentlink',
            name='target_invoice',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name='business_charge_links', to='documents.formaldocument', verbose_name='חשבונית פתוחה לסגירה',
            ),
        ),
        migrations.AddField(
            model_name='paymentlinkpayment',
            name='document_error',
            field=models.TextField(
                blank=True,
                help_text='A verified charge is never hidden when automatic document issuance needs office attention.',
            ),
        ),
    ]
