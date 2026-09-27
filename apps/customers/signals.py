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
    Create a ChildStatusHistory record when a child leaves 'active' for any
    other status, or arrives at 'inactive' from any status.

    The dashboard's dropout figure counts children who moved to 'inactive'.
    Recording only moves out of 'active' missed a real way to leave: a card
    fails (active → payment_problem) and the child then goes
    (payment_problem → inactive). Only the morning status fix, which writes a
    row of its own, ever recorded that second step; the office doing it by
    hand left no trace. The dashboard counts each child once, so the morning
    fix's row beside this one is not a double count.
    """
    # Import here to avoid circular import
    from apps.customers.status_history_models import ChildStatusHistory
    
    # Skip if this is a new child (no previous status)
    if created:
        return
    
    # Check if status actually changed
    previous_status = getattr(instance, '_previous_status', None)
    if previous_status and previous_status != instance.status:
        if previous_status == 'active' or instance.status == 'inactive':
            ChildStatusHistory.objects.create(
                child=instance,
                previous_status=previous_status,
                new_status=instance.status
            )


@receiver(post_save, sender=Child)
def fold_duplicates_of_a_registered_child(sender, instance, **kwargs):
    """
    A registered child just saved may be one the system already holds — as a
    walk-in the instructor added, or as an earlier registration. Fold those in
    now, so a ghost goes the moment the real child exists rather than lingering
    in the customers list.

    After commit, and never raising: this rides on registration and payment
    flows, and a duplicate that survives until the nightly sweep is a far
    smaller cost than a registration that fails because tidying up did.
    """
    from django.db import transaction

    from apps.customers.child_merge import merging, resolve_around

    if instance.status == 'ghost' or merging.get():
        return

    child_id = instance.pk

    def run():
        try:
            child = Child.objects.select_related('family').filter(pk=child_id).first()
            if child is not None:
                resolve_around(child)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('folding duplicates of child %s failed', child_id)

    transaction.on_commit(run)
