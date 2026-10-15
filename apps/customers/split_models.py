"""One child's monthly payment for one class, split between up to three cards.

Owner, 15.10.2026: parents who live apart each pay a part — ₪260 a month is
₪130 on each card. The split is made from the CRM only (the card link and the
office registration); the widget stays one card.

A split is a set of shares. Each share is one payer: their own card link,
their own standing order, their own charge every month and their own receipt.
The full figure — catalogue price, discounts, a special price, the
registration fee — is worked out once, when the split is made, exactly as a
one-card link would work it out, and is then divided; the shares add up to it
to the agora.

`RecurringPayment.split_share` and `Payment.split_share` say which share a
standing order or a charge belongs to: that is how the checks that look for
two standing orders on one class, or a class paid twice in a month, tell a
split from a mistake.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models

MAX_SPLIT_SHARES = 3


class PaymentSplit(models.Model):
    """חלוקת תשלום — one child's monthly payment for one class, divided between payers."""

    # A sign-up paid in parts from the first charge.
    MODE_NEW = 'new'
    # A child who already pays on one card: that card stays as the first payer,
    # the others join from the next billing date.
    MODE_EXISTING = 'existing'
    MODE_CHOICES = [(MODE_NEW, 'רישום חדש'), (MODE_EXISTING, 'הוראת קבע קיימת')]

    STATUS_ACTIVE = 'active'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [(STATUS_ACTIVE, 'פעילה'), (STATUS_CANCELLED, 'בוטלה')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    child = models.ForeignKey('customers.Child', on_delete=models.CASCADE, related_name='payment_splits', verbose_name="ילד")
    lesson = models.ForeignKey('courses.Lesson', on_delete=models.SET_NULL, null=True, blank=True, related_name='payment_splits', verbose_name="שיעור")
    bundle = models.ForeignKey('courses.LessonBundle', on_delete=models.SET_NULL, null=True, blank=True, related_name='payment_splits', verbose_name="מסלול")
    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default=MODE_NEW, verbose_name="סוג")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE, verbose_name="סטטוס")
    # The whole, as one card would have paid it.
    monthly_total = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="סכום חודשי מלא")
    base_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="מחיר לפני הנחות")
    registration_fee_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="דמי רישום מלאים")
    # The discount lines of the whole ({name, type, value, amount_deducted, reason}).
    discount_details = models.JSONField(default=list, blank=True, verbose_name="הנחות")
    special_monthly_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name="מחיר מיוחד")
    include_registration_fee = models.BooleanField(default=True, verbose_name="כולל דמי רישום")
    # MODE_EXISTING: the standing order that was paying the whole and becomes the first share.
    source_recurring = models.ForeignKey(
        'customers.RecurringPayment', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='splits_made_from', verbose_name="הוראת הקבע המקורית",
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='payment_splits_created')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    cancelled_at = models.DateTimeField(null=True, blank=True, verbose_name="בוטלה בתאריך")

    class Meta:
        db_table = 'payment_splits'
        verbose_name = "חלוקת תשלום"
        verbose_name_plural = "חלוקות תשלום"
        ordering = ['-created_at']

    def __str__(self):
        return f'חלוקה {self.child_id} · ₪{self.monthly_total}'

    @property
    def parts(self) -> int:
        return self.shares.count()


class PaymentSplitShare(models.Model):
    """חלק בחלוקה — one payer: their part of the month and of the registration fee."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    split = models.ForeignKey(PaymentSplit, on_delete=models.CASCADE, related_name='shares', verbose_name="חלוקה")
    position = models.PositiveSmallIntegerField(verbose_name="מספר משלם")
    # A parent of the family, or a name and phone typed for this share only —
    # such a payer is not added to the family, so broadcasts do not reach them.
    payer_parent = models.ForeignKey('customers.Parent', on_delete=models.SET_NULL, null=True, blank=True, related_name='payment_split_shares', verbose_name="הורה משלם")
    payer_name = models.CharField(max_length=200, verbose_name="שם המשלם")
    payer_phone = models.CharField(max_length=20, blank=True, verbose_name="טלפון המשלם")
    payer_email = models.EmailField(blank=True, verbose_name="אימייל המשלם")
    monthly_amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="סכום חודשי")
    registration_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), verbose_name="חלק בדמי הרישום")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")

    class Meta:
        db_table = 'payment_split_shares'
        verbose_name = "חלק בחלוקת תשלום"
        verbose_name_plural = "חלקים בחלוקת תשלום"
        ordering = ['split', 'position']
        constraints = [
            models.UniqueConstraint(fields=['split', 'position'], name='payment_split_share_position_unique'),
        ]

    def __str__(self):
        return f'{self.payer_name} · ₪{self.monthly_amount}'

    @property
    def label(self) -> str:
        """'חלק 1 מתוך 2' — written on charges, receipts and the parent's page."""
        return f'חלק {self.position} מתוך {self.split.shares.count()}'
