"""What the office decided about business customers that looked like one customer.

After the cards came over from the previous software, some customers are there
twice: one studio with a card per branch it rents in, a customer who changed
the name they trade under. The office merges those into one card — or says
they are different customers after all. Each decision is kept here: a merge
with everything the cards that went away carried, so nothing a card knew is
lost with it; a "different customers" so the same question is not asked again.

A card that was never a business customer — a pupil of the lessons the
previous software had billed by hand — is taken off the list the same way: the
whole card is kept here, and only then does it go.
"""
import uuid

from django.conf import settings
from django.db import models


class BusinessCustomerCleanup(models.Model):
    """One decision about a group of cards. Written once, never edited."""

    ACTION_MERGED = 'merged'
    ACTION_KEPT_APART = 'kept_apart'
    ACTION_REMOVED = 'removed'
    ACTION_CHOICES = [
        (ACTION_MERGED, 'אוחדו לכרטיס אחד'),
        (ACTION_KEPT_APART, 'לקוחות שונים'),
        (ACTION_REMOVED, 'אינם לקוחות עסקיים'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    action = models.CharField(max_length=12, choices=ACTION_CHOICES, verbose_name="ההחלטה")
    # A merge: the card that stayed. SET_NULL, so the record outlives the card.
    survivor = models.ForeignKey(
        'customers.BusinessCustomer', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='cleanups', verbose_name="הכרטיס שנשאר",
    )
    # Every card of the group, the survivor included, as ids — what "kept apart" is matched by.
    card_ids = models.JSONField(default=list, verbose_name="הכרטיסים")
    # A merge or a removal: each card that went away, every field it had, and its name.
    merged_cards = models.JSONField(default=list, blank=True, verbose_name="הכרטיסים שאוחדו")
    # A merge: how many rows of each kind moved to the survivor — {'documents.FormalDocument': 3, …}.
    moved = models.JSONField(default=dict, blank=True, verbose_name="מה עבר לכרטיס שנשאר")
    name_before = models.CharField(max_length=210, blank=True, verbose_name="השם לפני")
    name_after = models.CharField(max_length=210, blank=True, verbose_name="השם אחרי")

    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='business_customer_cleanups', verbose_name="מי החליט",
    )
    decided_by_name = models.CharField(max_length=150, blank=True, verbose_name="שם המחליט")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="מתי")

    class Meta:
        db_table = 'business_customer_cleanups'
        verbose_name = "סידור לקוחות עסקיים"
        verbose_name_plural = "סידור לקוחות עסקיים"
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_action_display()}: {self.name_after or len(self.card_ids)}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError('A cleanup decision is never edited')
        super().save(*args, **kwargs)
