"""
Customer Signals
Automatically track status changes when Child.status is updated
"""
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from apps.customers.models import Child


@receiver(pre_save, sender=Child)
def track_status_change(sender, instance, **kwargs):
    """
    Track status changes before saving Child model
    Store previous status in instance for post_save signal
    """
    if instance.pk:  # Only for existing records (not new creates)
        try:
            old_instance = Child.objects.get(pk=instance.pk)
            instance._previous_status = old_instance.status
        except Child.DoesNotExist:
            instance._previous_status = None
    else:
        instance._previous_status = None


@receiver(post_save, sender=Child)
def create_status_history(sender, instance, created, **kwargs):
    """
    Create ChildStatusHistory record when status changes FROM 'active' TO any other status
    Only tracks when children leave active status (quit/churn tracking)
    """
    # Import here to avoid circular import
    from apps.customers.status_history_models import ChildStatusHistory
    
    # Skip if this is a new child (no previous status)
    if created:
        return
    
    # Check if status actually changed
    previous_status = getattr(instance, '_previous_status', None)
    if previous_status and previous_status != instance.status:
        # Only save history when changing FROM 'active' TO any other status
        if previous_status == 'active' and instance.status != 'active':
            ChildStatusHistory.objects.create(
                child=instance,
                previous_status=previous_status,
                new_status=instance.status
            )
