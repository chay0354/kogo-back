from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('customers', '0005_payment_bundle'),
    ]

    operations = [
        migrations.AddField(
            model_name='payment',
            name='trial_lesson_date',
            field=models.DateField(
                blank=True,
                help_text='מוגדר על תשלום ממתין לשיעור ניסיון בתשלום; ההרשמה נוצרת לאחר סליקה מוצלחת.',
                null=True,
                verbose_name='תאריך שיעור ניסיון',
            ),
        ),
    ]
