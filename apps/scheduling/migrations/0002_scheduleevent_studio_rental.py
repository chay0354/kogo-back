# Generated manually for studio rental fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='scheduleevent',
            name='is_studio_rental',
            field=models.BooleanField(
                default=False,
                help_text='When true, price_per_session is counted as revenue per occurrence in dashboards.',
                verbose_name='שכירות סטודיו',
            ),
        ),
        migrations.AddField(
            model_name='scheduleevent',
            name='renter_name',
            field=models.CharField(blank=True, max_length=200, verbose_name='שם השוכר'),
        ),
        migrations.AddField(
            model_name='scheduleevent',
            name='price_per_session',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text='Revenue per rental occurrence (one_time = once; weekly = each week in range).',
                max_digits=10,
                verbose_name='מחיר למופע',
            ),
        ),
    ]
