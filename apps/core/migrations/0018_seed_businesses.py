from django.db import migrations

# The vocabulary the document dialog carried in code until now.
BUSINESSES = ['לקוחות', 'סוחרים', 'ספקים', 'חוגים', 'מותג קוגומלו', 'מותג געגע']


def seed(apps, schema_editor):
    Business = apps.get_model('core', 'Business')
    for order, name in enumerate(BUSINESSES):
        Business.objects.get_or_create(name=name, defaults={'sort_order': order})


class Migration(migrations.Migration):
    dependencies = [('core', '0017_business_taxonomy')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
