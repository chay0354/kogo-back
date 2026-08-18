from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('courses', '0014_lessonpriceoption'),
        ('customers', '0009_payment_registration_fee'),
    ]

    operations = [
        migrations.AddField(
            model_name='payment',
            name='price_option',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='payments',
                to='courses.lessonpriceoption',
                verbose_name='מחיר נוסף',
                help_text='Set when the parent chose an extra catalog price for this lesson in the widget.',
            ),
        ),
    ]
