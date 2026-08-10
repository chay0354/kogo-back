"""Push stock changes to the B2C website when a linked product is updated."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.store.models import StoreProduct, StoreProductSize
from apps.store.website_integration import push_product_to_website


@receiver(post_save, sender=StoreProduct)
def push_store_product_to_website(sender, instance: StoreProduct, **kwargs):
    if instance.website_legacy_id:
        push_product_to_website(instance)


@receiver(post_save, sender=StoreProductSize)
def push_size_stock_to_website(sender, instance: StoreProductSize, **kwargs):
    product = instance.product
    if product.website_legacy_id:
        product.recalculate_total_stock(save=True)
        push_product_to_website(product)
