from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('customers', '0006_payment_trial_lesson_date'),
    ]

    operations = [
        migrations.AddField(
            model_name='recurringpayment',
            name='pending_amount',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text='סכום חודשי שיחול מהמחזור הבא (לא מהחיוב הנוכחי).',
                max_digits=10,
                null=True,
                verbose_name='סכום מתוכנן',
            ),
        ),
        migrations.AddField(
            model_name='recurringpayment',
            name='pending_amount_effective_date',
            field=models.DateField(
                blank=True,
                help_text='תאריך שבו הסכום המתוכנן יחליף את הסכום הנוכחי.',
                null=True,
                verbose_name='תאריך תחילת סכום מתוכנן',
            ),
        ),
    ]
