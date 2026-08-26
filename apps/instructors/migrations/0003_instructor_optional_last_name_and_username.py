from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('instructors', '0002_remove_snapshot_models'),
    ]

    operations = [
        migrations.AlterField(
            model_name='instructor',
            name='last_name',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name='שם משפחה'),
        ),
        migrations.AlterField(
            model_name='instructor',
            name='email',
            field=models.CharField(blank=True, default='', max_length=254, verbose_name='שם משתמש'),
        ),
    ]
