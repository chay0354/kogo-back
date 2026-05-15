import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0002_storeproductsize'),
    ]

    operations = [
        migrations.AddField(
            model_name='storeproductsize',
            name='branch',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='store_product_sizes',
                to='core.branch',
                verbose_name='סניף',
                help_text='מיקום מלאי למידה זו (ריק = משלוח)',
            ),
        ),
    ]
