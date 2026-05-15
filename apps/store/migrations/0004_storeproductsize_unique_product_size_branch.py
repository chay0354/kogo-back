from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0003_storeproductsize_branch'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='storeproductsize',
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name='storeproductsize',
            constraint=models.UniqueConstraint(
                fields=('product', 'size', 'branch'),
                name='store_product_sizes_uniq_product_size_branch',
            ),
        ),
    ]
