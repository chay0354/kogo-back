"""Push stock changes to the B2C website when a linked product is updated."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.store.models import StoreProduct, StoreProductSize
from apps.store.website_integration import schedule_product_push


@receiver(post_save, sender=StoreProduct)
def push_store_product_to_website(sender, instance: StoreProduct, **kwargs):
    schedule_product_push(instance)


@receiver(post_save, sender=StoreProductSize)
def push_size_stock_to_website(sender, instance: StoreProductSize, **kwargs):
    product = instance.product
    if not product.website_legacy_id:
        return
    # The retotal saves the product, whose own post_save schedules the push;
    # scheduling here too is harmless — the batch keeps one entry per product.
    product.recalculate_total_stock(save=True)
    schedule_product_push(product)
