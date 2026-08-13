from django.db import migrations, models

from apps.core.registration_terms_default import DEFAULT_REGISTRATION_TERMS_HTML


def seed_default_terms(apps, schema_editor):
    RegistrationTerms = apps.get_model('core', 'RegistrationTerms')
    RegistrationTerms.objects.get_or_create(
        pk=1,
        defaults={'content': DEFAULT_REGISTRATION_TERMS_HTML},
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_change_external_link_to_charfield'),
    ]

    operations = [
        migrations.CreateModel(
            name='RegistrationTerms',
            fields=[
                ('id', models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ('content', models.TextField(verbose_name='תוכן HTML')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='עודכן בתאריך')),
            ],
            options={
                'verbose_name': 'תקנון רישום',
                'verbose_name_plural': 'תקנון רישום',
                'db_table': 'registration_terms',
            },
        ),
        migrations.RunPython(seed_default_terms, migrations.RunPython.noop),
    ]
