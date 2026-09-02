import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0016_integration_credential'),
        ('courses', '0019_course_show_in_widget'),
        ('customers', '0011_cronheartbeat'),
        ('documents', '0002_alter_documentcounter_unique_together_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='CheckPlan',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('description', models.CharField(blank=True, max_length=300, verbose_name='תיאור')),
                ('status', models.CharField(choices=[('active', 'פעיל'), ('completed', 'הושלם'), ('cancelled', 'בוטל')], default='active', max_length=20, verbose_name='סטטוס')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='תאריך יצירה')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='תאריך עדכון')),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='check_plans', to='core.branch', verbose_name='סניף')),
                ('child', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='check_plans', to='customers.child', verbose_name='ילד')),
                ('lesson', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='check_plans', to='courses.lesson', verbose_name='שיעור')),
                ('receipt', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='check_plan_receipts', to='documents.formaldocument', verbose_name='קבלה')),
            ],
            options={
                'verbose_name': "תוכנית צ'קים",
                'verbose_name_plural': "תוכניות צ'קים",
                'db_table': 'check_plans',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='CheckItem',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('due_date', models.DateField(verbose_name="תאריך צ'ק")),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12, verbose_name='סכום')),
                ('bank', models.CharField(blank=True, max_length=100, verbose_name='בנק')),
                ('bank_branch', models.CharField(blank=True, max_length=50, verbose_name='סניף בנק')),
                ('account_number', models.CharField(blank=True, max_length=50, verbose_name='מספר חשבון')),
                ('check_number', models.CharField(blank=True, max_length=50, verbose_name="מספר צ'ק")),
                ('status', models.CharField(choices=[('pending', 'ממתין'), ('invoiced', 'הופקה חשבונית'), ('cancelled', 'בוטל')], default='pending', max_length=20, verbose_name='סטטוס')),
                ('invoiced_at', models.DateTimeField(blank=True, null=True, verbose_name='תאריך הפקת חשבונית')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='תאריך יצירה')),
                ('plan', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='documents.checkplan', verbose_name='תוכנית')),
                ('tax_invoice', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='check_item_invoices', to='documents.formaldocument', verbose_name='חשבונית מס')),
            ],
            options={
                'verbose_name': "צ'ק",
                'verbose_name_plural': "צ'קים",
                'db_table': 'check_items',
                'ordering': ['due_date', 'created_at'],
            },
        ),
    ]
