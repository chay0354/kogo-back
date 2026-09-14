from django.db import migrations

# A business with no categories is a business no invoice can be issued for: the
# document wizard will not move past the business-customer step until a category
# is picked, and the seeded businesses arrived with none. Every business gets a
# 'כללי' to fall back on — rename it or switch it off in הגדרות ← כספים once the
# real categories are in.
NEW_BUSINESS = 'הצגות חיצוניות'
FALLBACK_CATEGORY = 'כללי'


def seed(apps, schema_editor):
    Business = apps.get_model('core', 'Business')
    BusinessCategory = apps.get_model('core', 'BusinessCategory')

    last = Business.objects.order_by('-sort_order').values_list('sort_order', flat=True).first()
    Business.objects.get_or_create(name=NEW_BUSINESS, defaults={'sort_order': (last or 0) + 1})

    for business in Business.objects.all():
        if not BusinessCategory.objects.filter(business=business).exists():
            BusinessCategory.objects.create(business=business, name=FALLBACK_CATEGORY)


class Migration(migrations.Migration):
    dependencies = [('core', '0020_terms_computerized_docs_clause')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
