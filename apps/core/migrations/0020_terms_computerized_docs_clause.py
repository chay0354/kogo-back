"""Put the consent to computerized documents in the registration terms themselves.

The widget asked for it with a checkbox of its own; the owner wants it as one
more line of the terms the parent already reads, scrolls through and accepts.
Accepting the terms is then the consent (סעיף 18ב(ג)), recorded on the family
as before.

The terms are one row the office edits in the CRM settings, so the paragraph is
added to whatever the row holds now — before the closing badge when it has one —
and only when the terms don't speak of it already. A database with no row gets
the paragraph with the default text (registration_terms_default.py).
"""
from django.db import migrations

CLAUSE = (
    '<p><strong>מסמכים ממוחשבים:</strong> הנני מסכים/ה לקבל חשבוניות, קבלות והודעות זיכוי '
    'בדוא״ל, כמסמך ממוחשב.</p>'
)
MARKER = 'מסמכים ממוחשבים'
BADGE = '<p class="terms-badge">'


def add_clause(apps, schema_editor):
    RegistrationTerms = apps.get_model('core', 'RegistrationTerms')
    terms = RegistrationTerms.objects.filter(pk=1).first()
    if terms is None or MARKER in terms.content:
        return
    content = terms.content
    at = content.rfind(BADGE)
    if at == -1:
        content = f'{content.rstrip()}\n{CLAUSE}'
    else:
        content = f'{content[:at]}{CLAUSE}\n{content[at:]}'
    terms.content = content
    terms.save(update_fields=['content', 'updated_at'])


def remove_clause(apps, schema_editor):
    RegistrationTerms = apps.get_model('core', 'RegistrationTerms')
    terms = RegistrationTerms.objects.filter(pk=1).first()
    if terms is None:
        return
    for piece in (f'{CLAUSE}\n', f'\n{CLAUSE}', CLAUSE):
        if piece in terms.content:
            terms.content = terms.content.replace(piece, '', 1)
            terms.save(update_fields=['content', 'updated_at'])
            return


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0019_seed_delivery_category'),
    ]

    operations = [
        migrations.RunPython(add_clause, remove_clause),
    ]
