from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0008_website_integration'),
    ]

    operations = [
        migrations.AddField(
            model_name='storeproduct',
            name='branch_only',
            field=models.BooleanField(
                default=False,
                help_text='When True, the B2C shop shows this product as branch-only (not purchasable online)',
                verbose_name='לסניפים בלבד',
            ),
        ),
    ]
