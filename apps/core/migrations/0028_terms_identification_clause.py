"""
Add the identification paragraph to the registration terms.

The registration form can recognise a returning parent by identity number and
phone and show the children's first names (apps/customers/
widget_identification.py). It does so only for a family whose parent accepted
terms that say so — this paragraph. Accepting the terms from now on records
that consent on the family; nobody is recognised before they sign again.

Like 0020: the office edits the terms freely, so the paragraph is added once,
by its title, before the closing badge — and a fresh database is seeded from
the default, which already carries it.
"""
from django.db import migrations


CLAUSE = (
    '<p><strong>זיהוי בהרשמה הבאה:</strong> בהרשמה הבאה קוגומלו תזהה אותי לפי תעודת זהות וטלפון, '
    'ותציג את השמות הפרטיים של ילדיי כדי למלא את הטופס בשבילי.</p>'
)
MARKER = 'זיהוי בהרשמה הבאה'
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
        ('core', '0027_card_payout_terminal_month'),
    ]

    operations = [
        migrations.RunPython(add_clause, remove_clause),
    ]
