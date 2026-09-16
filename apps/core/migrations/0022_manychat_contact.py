from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0021_business_default_categories'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ManyChatContact',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('phone', models.CharField(max_length=20, unique=True, verbose_name='טלפון (בינלאומי)')),
                ('subscriber_id', models.BigIntegerField(verbose_name='מזהה איש קשר ב-ManyChat')),
                ('source', models.CharField(choices=[('found', 'נמצא אוטומטית'), ('created', 'נוצר על ידי המערכת'), ('manual', 'קושר ידנית')], max_length=16, verbose_name='מקור')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='תאריך יצירה')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='תאריך עדכון')),
                ('linked_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='manychat_contacts_linked', to=settings.AUTH_USER_MODEL, verbose_name='קושר על ידי')),
            ],
            options={
                'verbose_name': 'איש קשר ב-ManyChat',
                'verbose_name_plural': 'אנשי קשר ב-ManyChat',
                'db_table': 'manychat_contacts',
            },
        ),
    ]
