from django.db import migrations

# A website delivery is merchandise sold by the brand, not by a branch. The
# dashboard had this as a label only; the report needs a real category so a
# document tagged with it and a delivery land on the same line.
DELIVERY_BUSINESS = 'מותג קוגומלו'
DELIVERY_CATEGORY = 'מרצנדייס משלוחים'


def seed(apps, schema_editor):
    Business = apps.get_model('core', 'Business')
    BusinessCategory = apps.get_model('core', 'BusinessCategory')
    business, _ = Business.objects.get_or_create(name=DELIVERY_BUSINESS, defaults={'sort_order': 4})
    BusinessCategory.objects.get_or_create(business=business, name=DELIVERY_CATEGORY)


class Migration(migrations.Migration):
    dependencies = [('core', '0018_seed_businesses')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
