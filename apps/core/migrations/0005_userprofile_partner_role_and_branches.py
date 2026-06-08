from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_branchmonthlysnapshot_base_revenue_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='userprofile',
            name='role',
            field=models.CharField(
                choices=[
                    ('manager', 'Manager'),
                    ('worker', 'Worker'),
                    ('partner', 'Partner'),
                ],
                default='worker',
                max_length=20,
                verbose_name='תפקיד',
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='assigned_branches',
            field=models.ManyToManyField(
                blank=True,
                help_text='סניפים שהשותף רשאי לראות ולנהל',
                related_name='assigned_partners',
                to='core.branch',
                verbose_name='סניפים משויכים',
            ),
        ),
    ]
