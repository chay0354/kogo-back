"""A tenant's card and monthly standing order, and the charges it makes.

    TenantStandingOrder  — what a tenancy is billed each month, on which card
    TenantCharge         — one billing month of one order: reserved, then charged,
                           failed or sent to review; its receipt hangs off it
    TenantCardLink       — the public link a tenant enters their card through

The rules that keep a card from being charged twice live in billing.py; the
database holds the ones it can hold on its own:

* UNIQUE(standing_order, period) on TenantCharge. The row is committed as
  'reserved' before Tranzila is called, so a second run, a retry or a double
  click finds the month taken and never reaches the gateway.
* One open standing order per tenancy. Two orders on one tenancy would each
  bill the same month under a different key, which the guard above cannot see.
* total = amount_before_vat + vat_amount, all in agorot, and the period is the
  first of a month.

Card numbers are never stored. What Tranzila hands back is a token (charged on
the token terminal the courses use), its expiry and the last four digits.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from apps.rentals.models import BILLING_DAY_MAX, BILLING_DAY_MIN


class TenantStandingOrder(models.Model):
    """הוראת קבע של שוכר — the monthly amount, the billing day, and the card it is charged on."""

    STATUS_PENDING_CARD = 'pending_card'
    STATUS_ACTIVE = 'active'
    STATUS_PAUSED = 'paused'
    STATUS_FAILED = 'failed'
    STATUS_ENDED = 'ended'
    STATUS_CHOICES = [
        (STATUS_PENDING_CARD, 'ממתינה לכרטיס'),
        (STATUS_ACTIVE, 'פעילה'),
        (STATUS_PAUSED, 'מושהית'),
        (STATUS_FAILED, 'החיוב נכשל'),
        (STATUS_ENDED, 'הסתיימה'),
    ]
    # Everything but ended: an order that may still charge, or be made to.
    OPEN_STATUSES = (STATUS_PENDING_CARD, STATUS_ACTIVE, STATUS_PAUSED, STATUS_FAILED)

    SOURCE_SIGNING = 'signing'
    SOURCE_OFFICE = 'office'
    SOURCE_CHOICES = [
        (SOURCE_SIGNING, 'חתימה על החוזה'),
        (SOURCE_OFFICE, 'המשרד'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PROTECT: an order that charged a card is part of the tenancy's financial record.
    tenancy = models.ForeignKey(
        'rentals.Tenancy', on_delete=models.PROTECT, related_name='standing_orders', verbose_name='הסכם שכירות',
    )
    tenant = models.ForeignKey(
        'customers.BusinessCustomer', on_delete=models.PROTECT, related_name='rental_standing_orders',
        verbose_name='שוכר',
    )
    branch = models.ForeignKey(
        'core.Branch', on_delete=models.SET_NULL, null=True, blank=True, related_name='rental_standing_orders',
        verbose_name='סניף',
    )
    # The income tag, copied to every charge and receipt. The business is found
    # by name (RENTAL_BILLING_BUSINESS_NAME) and may be missing when the order is
    # created; billing refuses to charge until it exists.
    business = models.ForeignKey(
        'core.Business', on_delete=models.PROTECT, null=True, blank=True, related_name='+', verbose_name='עסק',
    )
    business_category = models.ForeignKey(
        'core.BusinessCategory', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        verbose_name='קטגוריה בעסק',
    )
    amount_before_vat = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='סכום חודשי (לפני מע"מ)')
    billing_day = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(BILLING_DAY_MIN), MaxValueValidator(BILLING_DAY_MAX)],
        verbose_name='יום חיוב בחודש',
    )
    start_date = models.DateField(verbose_name='תחילת החיוב')
    end_date = models.DateField(null=True, blank=True, verbose_name='סיום החיוב')
    # The day the next month is charged on or after. Empty until a card is on file.
    next_charge_date = models.DateField(null=True, blank=True, verbose_name='החיוב הבא')
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING_CARD, verbose_name='סטטוס',
    )
    tranzila_token = models.CharField(max_length=200, blank=True, verbose_name='טוקן כרטיס בטרנזילה')
    card_expire_month = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name='חודש תוקף')
    card_expire_year = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name='שנת תוקף')
    card_last4 = models.CharField(max_length=4, blank=True, verbose_name='4 ספרות אחרונות')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_OFFICE, verbose_name='מקור')
    # Why the order stopped charging, when it did. Cleared by the next success.
    last_error = models.TextField(blank=True, verbose_name='שגיאה אחרונה')
    failed_at = models.DateTimeField(null=True, blank=True, verbose_name='מועד הכישלון')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        verbose_name='נוצר על ידי',
    )
    notes = models.TextField(blank=True, verbose_name='הערות')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='תאריך יצירה')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='תאריך עדכון')

    class Meta:
        db_table = 'tenant_standing_orders'
        verbose_name = 'הוראת קבע של שוכר'
        verbose_name_plural = 'הוראות קבע של שוכרים'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status', 'next_charge_date'], name='tenant_so_due_idx')]
        constraints = [
            # Stricter than one live order per tenancy and branch: a tenancy
            # decides its branch, and an order left open on a tenancy that moved
            # branch would bill the same months as the new one.
            models.UniqueConstraint(
                fields=['tenancy'],
                condition=Q(status__in=['pending_card', 'active', 'paused', 'failed']),
                name='tenant_standing_order_one_open_per_tenancy',
            ),
            models.CheckConstraint(
                check=Q(billing_day__gte=BILLING_DAY_MIN, billing_day__lte=BILLING_DAY_MAX),
                name='tenant_standing_order_billing_day_in_month',
            ),
            models.CheckConstraint(
                check=Q(amount_before_vat__gt=0), name='tenant_standing_order_amount_positive',
            ),
            models.CheckConstraint(
                check=Q(end_date__isnull=True) | Q(end_date__gte=F('start_date')),
                name='tenant_standing_order_ends_after_start',
            ),
        ]

    def __str__(self):
        return f'{self.tenant} · {self.get_status_display()}'

    @property
    def has_card(self) -> bool:
        return bool(self.tranzila_token and self.card_expire_month and self.card_expire_year)


class TenantCharge(models.Model):
    """חיוב חודשי של שוכר — one billing month of one standing order."""

    STATUS_RESERVED = 'reserved'
    STATUS_CHARGED = 'charged'
    STATUS_FAILED = 'failed'
    STATUS_REVIEW = 'review'
    STATUS_VOIDED = 'voided'
    STATUS_CHOICES = [
        (STATUS_RESERVED, 'שמור לחיוב'),
        (STATUS_CHARGED, 'חויב'),
        (STATUS_FAILED, 'נדחה'),
        # Tranzila's answer is unknown (a timeout, a crash between the call and
        # the write). Never retried by itself: the office checks and decides.
        (STATUS_REVIEW, 'בבדיקה'),
        (STATUS_VOIDED, 'בוטל'),
    ]

    TRIGGER_CRON = 'cron'
    TRIGGER_CARD = 'card'
    TRIGGER_RETRY = 'retry'
    TRIGGER_CHOICES = [
        (TRIGGER_CRON, 'חיוב חודשי'),
        (TRIGGER_CARD, 'הזנת כרטיס'),
        (TRIGGER_RETRY, 'ניסיון חוזר מהמשרד'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    standing_order = models.ForeignKey(
        TenantStandingOrder, on_delete=models.PROTECT, related_name='charges', verbose_name='הוראת קבע',
    )
    # The billing month, as its first day.
    period = models.DateField(verbose_name='חודש החיוב')
    # In agorot, computed with apps.core.vat when the month is reserved.
    amount_before_vat = models.PositiveIntegerField(verbose_name='לפני מע"מ (אגורות)')
    vat_amount = models.PositiveIntegerField(verbose_name='מע"מ (אגורות)')
    total = models.PositiveIntegerField(verbose_name='סה"כ (אגורות)')
    # The income tag, copied from the order when the month is reserved.
    business = models.ForeignKey(
        'core.Business', on_delete=models.PROTECT, related_name='+', verbose_name='עסק',
    )
    business_category = models.ForeignKey(
        'core.BusinessCategory', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        verbose_name='קטגוריה בעסק',
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_RESERVED, verbose_name='סטטוס')
    trigger = models.CharField(max_length=20, choices=TRIGGER_CHOICES, verbose_name='מקור החיוב')
    # How many times this month was sent to Tranzila.
    attempts = models.PositiveSmallIntegerField(default=0, verbose_name='ניסיונות')
    card_last4 = models.CharField(max_length=4, blank=True, verbose_name='4 ספרות אחרונות')
    transaction_id = models.CharField(max_length=100, blank=True, verbose_name='מזהה עסקה בטרנזילה')
    confirmation_code = models.CharField(max_length=50, blank=True, verbose_name='מספר אישור')
    response_code = models.CharField(max_length=20, blank=True, verbose_name='קוד תשובה')
    error = models.TextField(blank=True, verbose_name='שגיאה')
    reserved_at = models.DateTimeField(default=timezone.now, verbose_name='מועד השמירה')
    charged_at = models.DateTimeField(null=True, blank=True, verbose_name='מועד החיוב')
    # PROTECT: a receipt that was issued for a charge is never taken from under it.
    receipt = models.OneToOneField(
        'documents.FormalDocument', on_delete=models.PROTECT, null=True, blank=True, related_name='tenant_charge',
        verbose_name='חשבונית מס/קבלה',
    )
    # A charge that went through and whose receipt did not: shown to the office as "charged, no receipt".
    receipt_error = models.TextField(blank=True, verbose_name='שגיאת הפקת קבלה')
    receipt_emailed_at = models.DateTimeField(null=True, blank=True, verbose_name='מועד שליחת הקבלה')
    # The office's decision on a charge in review, a failed or voided one.
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        verbose_name='הוכרע על ידי',
    )
    resolved_at = models.DateTimeField(null=True, blank=True, verbose_name='מועד ההכרעה')
    resolution_note = models.TextField(blank=True, verbose_name='הערת הכרעה')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='תאריך יצירה')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='תאריך עדכון')

    class Meta:
        db_table = 'tenant_charges'
        verbose_name = 'חיוב שוכר'
        verbose_name_plural = 'חיובי שוכרים'
        ordering = ['-period', '-created_at']
        indexes = [models.Index(fields=['status', 'reserved_at'], name='tenant_charge_status_idx')]
        constraints = [
            # The idempotency guard: one row per order and month, whatever its state.
            models.UniqueConstraint(fields=['standing_order', 'period'], name='tenant_charge_one_per_order_month'),
            models.CheckConstraint(check=Q(period__day=1), name='tenant_charge_period_first_of_month'),
            models.CheckConstraint(
                check=Q(total=F('amount_before_vat') + F('vat_amount')), name='tenant_charge_total_is_net_plus_vat',
            ),
        ]

    def __str__(self):
        return f'{self.standing_order_id} · {self.period:%Y-%m} · {self.get_status_display()}'


class TenantCardLink(models.Model):
    """קישור להזנת כרטיס — the public page a tenant enters a card through, for one standing order."""

    STATUS_PENDING = 'pending'
    STATUS_PROCESSING = 'processing'
    STATUS_USED = 'used'
    STATUS_CANCELLED = 'cancelled'
    STATUS_REVIEW = 'review'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'ממתין'),
        (STATUS_PROCESSING, 'בעיבוד'),
        (STATUS_USED, 'מומש'),
        (STATUS_CANCELLED, 'בוטל'),
        (STATUS_REVIEW, 'בבדיקה'),
    ]
    LIVE_STATUSES = (STATUS_PENDING, STATUS_PROCESSING)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    standing_order = models.ForeignKey(
        TenantStandingOrder, on_delete=models.PROTECT, related_name='card_links', verbose_name='הוראת קבע',
    )
    token = models.CharField(max_length=40, unique=True, verbose_name='טוקן')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, verbose_name='סטטוס')
    attempts = models.PositiveSmallIntegerField(default=0, verbose_name='ניסיונות')
    charge_started_at = models.DateTimeField(null=True, blank=True, verbose_name='תחילת העיבוד')
    used_at = models.DateTimeField(null=True, blank=True, verbose_name='מועד המימוש')
    last_error = models.TextField(blank=True, verbose_name='שגיאה אחרונה')
    review_reason = models.CharField(max_length=200, blank=True, verbose_name='סיבת הבדיקה')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        verbose_name='נוצר על ידי',
    )
    # The 14 days a link is good for run from here.
    created_at = models.DateTimeField(default=timezone.now, verbose_name='תאריך יצירה')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='תאריך עדכון')

    class Meta:
        db_table = 'tenant_card_links'
        verbose_name = 'קישור כרטיס לשוכר'
        verbose_name_plural = 'קישורי כרטיס לשוכרים'
        ordering = ['-created_at']
        constraints = [
            # One URL in play per order: rotating cancels the previous one first.
            models.UniqueConstraint(
                fields=['standing_order'],
                condition=Q(status__in=['pending', 'processing']),
                name='tenant_card_link_one_live_per_order',
            ),
        ]

    def __str__(self):
        return f'{self.standing_order_id} · {self.get_status_display()}'
