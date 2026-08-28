from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0016_course_display_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='course',
            name='registration_fee_override',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text='אם מוגדר, מחליף את דמי הרישום הכלליים רק לחוג זה.',
                max_digits=10,
                null=True,
                verbose_name='דמי רישום מותאמים (₪)',
            ),
        ),
        migrations.AddField(
            model_name='course',
            name='charge_standing_order_immediately',
            field=models.BooleanField(
                default=False,
                help_text='כאשר מופעל, הוראת הקבע של החוג הזה מחויבת מהיום ולא מתאריך תחילת העונה הכללי.',
                verbose_name='חיוב הוראת קבע מיידי',
            ),
        ),
    ]
