from django.db import migrations


def map_tags(apps, schema_editor):
    Business = apps.get_model('core', 'Business')
    BusinessCategory = apps.get_model('core', 'BusinessCategory')
    BusinessCustomer = apps.get_model('customers', 'BusinessCustomer')
    for customer in BusinessCustomer.objects.exclude(business_type=''):
        business = Business.objects.filter(name=customer.business_type.strip()).first()
        if business is None:
            continue
        customer.business = business
        category_name = (customer.category or '').strip()
        if category_name:
            customer.business_category, _ = BusinessCategory.objects.get_or_create(business=business, name=category_name)
        customer.save(update_fields=['business', 'business_category'])


class Migration(migrations.Migration):
    dependencies = [('customers', '0012_business_customer_tags'), ('core', '0018_seed_businesses')]
    operations = [migrations.RunPython(map_tags, migrations.RunPython.noop)]
