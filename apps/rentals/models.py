"""Tenancies — one rental agreement with one tenant, for the studios the branches rent out.

A studio rental used to live only on the calendar: a ScheduleEvent with
is_studio_rental=True that carries the renter's name, ID and price per session.
A tenant who rents two weekly slots is two events, and nothing said they were
one agreement with one person. A Tenancy is that agreement:

    tenant  — a BusinessCustomer, the office's merchant record (tagged סוחרים)
    slots   — the calendar events it covers (ScheduleEvent.tenancy)
    terms   — a monthly amount before VAT, the day of the month it is billed,
              and the dates the agreement runs

The calendar stays the source of truth for when a studio is taken and at what
rate. The tenancy never copies times or prices; it points at the events that
carry them, so the two cannot drift apart.

The status runs draft → sent → signed → active → ended, or cancelled at any
point. In this first phase the office moves it by hand; later phases (a signed
contract, a standing order) are meant to move it on their own.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.core.vat import add_vat

# The day of the month a charge lands on. Capped at 28 so every month has it.
BILLING_DAY_MIN = 1
BILLING_DAY_MAX = 28


class Tenancy(models.Model):
    """הסכם שכירות — one tenant, the studio slots they rent, and the monthly terms."""

    STATUS_DRAFT = 'draft'
    STATUS_SENT = 'sent'
    STATUS_SIGNED = 'signed'
    STATUS_ACTIVE = 'active'
    STATUS_ENDED = 'ended'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'טיוטה'),
        (STATUS_SENT, 'נשלח'),
        (STATUS_SIGNED, 'נחתם'),
        (STATUS_ACTIVE, 'פעיל'),
        (STATUS_ENDED, 'הסתיים'),
        (STATUS_CANCELLED, 'בוטל'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PROTECT: the tenant is part of the agreement's record. Deleting the
    # customer must neither take the agreement with it nor leave it naming nobody.
    tenant = models.ForeignKey(
        'customers.BusinessCustomer',
        on_delete=models.PROTECT,
        related_name='tenancies',
        verbose_name='שוכר',
    )
    branch = models.ForeignKey(
        'core.Branch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tenancies',
        verbose_name='סניף',
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT, verbose_name='סטטוס',
    )
    monthly_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0'))],
        verbose_name='סכום חודשי (לפני מע"מ)',
    )
    billing_day = models.PositiveSmallIntegerField(
        default=BILLING_DAY_MIN,
        validators=[MinValueValidator(BILLING_DAY_MIN), MaxValueValidator(BILLING_DAY_MAX)],
        verbose_name='יום חיוב בחודש',
    )
    start_date = models.DateField(null=True, blank=True, verbose_name='תחילת ההסכם')
    end_date = models.DateField(null=True, blank=True, verbose_name='סיום ההסכם')
    notes = models.TextField(blank=True, verbose_name='הערות')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='תאריך יצירה')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='תאריך עדכון')

    class Meta:
        db_table = 'tenancies'
        verbose_name = 'הסכם שכירות'
        verbose_name_plural = 'הסכמי שכירות'
        ordering = ['-created_at']
        constraints = [
            # The rules the API validates, held by the database as well, so the
            # admin or a script cannot store a day that some months do not have.
            models.CheckConstraint(
                check=models.Q(billing_day__gte=BILLING_DAY_MIN, billing_day__lte=BILLING_DAY_MAX),
                name='tenancy_billing_day_in_month',
            ),
            models.CheckConstraint(
                check=models.Q(monthly_amount__gte=0),
                name='tenancy_monthly_amount_not_negative',
            ),
        ]

    def __str__(self):
        return f'{self.tenant} · {self.get_status_display()}'

    @property
    def monthly_total(self) -> Decimal:
        """What the tenant pays each month: monthly_amount plus VAT at the current rate."""
        return add_vat(self.monthly_amount)
