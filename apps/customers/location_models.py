"""Where a business customer is filed, each time it was set — and what moved with it.

A business customer's location is three things together: the business, the
category in it, and (under the category סניפים) the branch. Every document
issued to the customer takes the business and category of that moment, so
changing the location is either a change from now on, or — when the office
says so — a change to the documents already issued too. This table is the
record of each change: who, when, from what to what, and, when documents were
moved, what each of them carried before, so the move can be read and undone.
"""
import uuid

from django.conf import settings
from django.db import models


class BusinessCustomerLocationChange(models.Model):
    """One change of a business customer's location. Written once, never edited."""

    # The first time a location is set: nothing to choose, nothing behind it.
    SCOPE_FIRST = 'first'
    # The card only: documents already issued keep the location they were issued under.
    SCOPE_FUTURE = 'future'
    # The card and every document already issued to the customer.
    SCOPE_ALL = 'all'
    SCOPE_CHOICES = [
        (SCOPE_FIRST, 'שיוך ראשון'),
        (SCOPE_FUTURE, 'מעכשיו והלאה'),
        (SCOPE_ALL, 'גם מסמכים שכבר הופקו'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        'customers.BusinessCustomer', on_delete=models.CASCADE,
        related_name='location_changes', verbose_name="לקוח עסקי",
    )
    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES, verbose_name="היקף השינוי")

    # Ids and names both: the names are what the office read on the day, and
    # they stay readable after a branch or a category is renamed or removed.
    previous_business_id = models.UUIDField(null=True, blank=True, verbose_name="העסק הקודם")
    previous_category_id = models.UUIDField(null=True, blank=True, verbose_name="הקטגוריה הקודמת")
    previous_branch_id = models.UUIDField(null=True, blank=True, verbose_name="הסניף הקודם")
    previous_label = models.CharField(max_length=320, blank=True, verbose_name="המיקום הקודם")
    new_business_id = models.UUIDField(null=True, blank=True, verbose_name="העסק החדש")
    new_category_id = models.UUIDField(null=True, blank=True, verbose_name="הקטגוריה החדשה")
    new_branch_id = models.UUIDField(null=True, blank=True, verbose_name="הסניף החדש")
    new_label = models.CharField(max_length=320, blank=True, verbose_name="המיקום החדש")

    # SCOPE_ALL: every document that was moved, with what it carried before —
    # [{id, number, business_id, business_category_id, branch_id}].
    documents = models.JSONField(default=list, blank=True, verbose_name="מסמכים ששויכו מחדש")
    documents_changed = models.PositiveIntegerField(default=0, verbose_name="מספר המסמכים ששויכו מחדש")

    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='business_customer_location_changes', verbose_name="מי שינה",
    )
    changed_by_name = models.CharField(max_length=150, blank=True, verbose_name="שם המשנה")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="מתי")

    class Meta:
        db_table = 'business_customer_location_changes'
        verbose_name = "שינוי מיקום של לקוח עסקי"
        verbose_name_plural = "שינויי מיקום של לקוחות עסקיים"
        ordering = ['-created_at']
        indexes = [models.Index(fields=['customer', '-created_at'], name='bc_location_change_customer')]

    def __str__(self):
        return f'{self.customer_id}: {self.previous_label or "—"} → {self.new_label} ({self.scope})'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError('A location change is never edited')
        super().save(*args, **kwargs)
