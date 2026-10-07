"""The business סניפים: a business customer's document can be a branch's income.

Owner, 7.10.2026: choosing a business for a business customer, he wants to be
able to choose "סניפים", then the branch, and have the income recorded in that
branch — and to add categories for branches in the settings. The businesses
and their categories are rows the office manages; this adds the one row the
choice needs. A business of that name that already exists is left as it is
(only switched on), and going back removes nothing: documents may be filed
under it by then.
"""
from django.db import migrations

NAME = 'סניפים'


# Every business starts with a category, so no screen that asks for one is a
# dead end (the others have "כללי" too). Under סניפים a category is optional;
# the office adds its own in the settings.
FIRST_CATEGORY = 'כללי'


def add_branches_business(apps, schema_editor):
    Business = apps.get_model('core', 'Business')
    BusinessCategory = apps.get_model('core', 'BusinessCategory')
    business, created = Business.objects.get_or_create(name=NAME, defaults={'is_active': True, 'sort_order': 0})
    if not created and not business.is_active:
        business.is_active = True
        business.save(update_fields=['is_active'])
    if not BusinessCategory.objects.filter(business=business).exists():
        BusinessCategory.objects.create(business=business, name=FIRST_CATEGORY, is_active=True, sort_order=0)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0030_em_hamoshavot_directions'),
    ]

    operations = [
        migrations.RunPython(add_branches_business, migrations.RunPython.noop),
    ]
