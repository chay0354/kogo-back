import django.core.validators
import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='StoreProductSize',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('size', models.CharField(help_text='Size label (e.g. S, M, L, 42)', max_length=20, verbose_name='מידה')),
                ('stock_quantity', models.IntegerField(default=0, help_text='Stock available for this specific size', validators=[django.core.validators.MinValueValidator(0)], verbose_name='כמות במלאי')),
                ('sort_order', models.PositiveIntegerField(default=0, help_text='Display order among other sizes of the same product', verbose_name='סדר')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='תאריך יצירה')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='תאריך עדכון')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='size_stocks', to='store.storeproduct', verbose_name='מוצר')),
            ],
            options={
                'verbose_name': 'מלאי לפי מידה',
                'verbose_name_plural': 'מלאי לפי מידה',
                'db_table': 'store_product_sizes',
                'ordering': ['sort_order', 'size'],
                'unique_together': {('product', 'size')},
                'indexes': [
                    models.Index(fields=['product'], name='store_produ_product_psize_idx'),
                    models.Index(fields=['product', 'size'], name='store_produ_product_size_idx'),
                ],
            },
        ),
    ]
