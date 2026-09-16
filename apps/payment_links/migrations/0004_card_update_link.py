"""
`card_update_links` — the trace a standing-order card link leaves behind.

One new table and nothing else. No existing table is touched, no column
changes meaning, and no row anywhere is rewritten: there is deliberately no
backfill, because the links already in the wild were never recorded and no
honest row can be invented for them. They keep working; they simply do not
appear on the new screen until a fresh link is made.

Every column outside the primary key is nullable or carries a default, for the
same reason `0003` gave: `vercel_build.py` runs `migrate` at build time while
the previous deployment is still answering requests, and that code knows
nothing about this table. It never writes here, and the new code only ever
writes here outside a money transaction — so a deployment in either direction
leaves charges exactly as they are.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('customers', '0017_computerized_docs_consent'),
        ('payment_links', '0003_card_link_short_token'),
    ]

    operations = [
        migrations.CreateModel(
            name='CardUpdateLink',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('mode', models.CharField(blank=True, max_length=20)),
                ('amount', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('months', models.JSONField(blank=True, default=list)),
                ('status', models.CharField(choices=[('created', 'נשלח'), ('opened', 'נפתח'), ('card_saved', 'כרטיס עודכן'), ('charged', 'חויב'), ('declined', 'נדחה')], default='created', max_length=12)),
                ('channel', models.CharField(blank=True, choices=[('whatsapp', 'וואטסאפ'), ('copy', 'העתקה')], max_length=12)),
                ('token', models.CharField(blank=True, db_index=True, max_length=512)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('sent_result', models.JSONField(blank=True, default=dict)),
                ('first_opened_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('charged_amount', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('last_error', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('child', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='card_update_links', to='customers.child')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='card_update_links_created', to=settings.AUTH_USER_MODEL)),
                ('recurring_payment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='card_update_links', to='customers.recurringpayment')),
            ],
            options={
                'db_table': 'card_update_links',
                'ordering': ['-created_at'],
            },
        ),
    ]
