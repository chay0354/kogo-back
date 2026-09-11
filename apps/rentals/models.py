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

A RentalContract is the other way round: one issued version of the tenancy's
contract, which copies what the tenancy said at that moment and the PDF drawn
from it, and never changes again (apps/rentals/contracts.py issues them).
"""
from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.core.vat import add_vat
from apps.scheduling.rental_agreement.terms import canonical_json, terms_sha256

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


def sha256_hex(data) -> str:
    """SHA-256, in hex, of stored bytes (bytes, or the memoryview the database hands back)."""
    return hashlib.sha256(bytes(data or b'')).hexdigest()


class FrozenContractError(Exception):
    """An attempt to change what an issued rental contract says. Issue a new version instead."""


# What a contract says, which contract it is and when it was issued: fixed
# from the moment it is saved.
CONTRACT_FROZEN_FIELDS = ('tenancy_id', 'version', 'terms', 'terms_sha256', 'pdf', 'pdf_sha256', 'created_at')
# Who issued it is fixed as well, except that it may be cleared: deleting the
# user's account sets it to NULL (on_delete=SET_NULL), and the contract outlives the account.
CONTRACT_ISSUER_FIELDS = ('created_by', 'created_by_id')


def _frozen_value(name, value):
    """A frozen field's value in one comparable form, however it was loaded or assigned."""
    if value is None:
        return None
    if name == 'pdf':
        return bytes(value)
    if name == 'terms':
        return canonical_json(value)
    if name == 'version':
        return int(value)
    if name == 'created_at':
        # Aware datetimes compare by the instant, whatever zone each was read in.
        return value
    return str(value)


def _refused_update_fields(values: dict) -> list[str]:
    """The fields in `values` that no change may touch: the frozen ones, and the issuer unless it is cleared."""
    return sorted(
        name for name, value in values.items()
        if name in CONTRACT_FROZEN_FIELDS or name == 'tenancy'
        or (name in CONTRACT_ISSUER_FIELDS and value is not None)
    )


class RentalContractQuerySet(models.QuerySet):
    def update(self, **kwargs):
        # A bulk update skips save(). It is refused the frozen fields as well,
        # so the rule has no side door in the ORM.
        frozen = _refused_update_fields(kwargs)
        if frozen:
            raise FrozenContractError(
                f'{", ".join(frozen)} of a rental contract never change once issued; issue a new version'
            )
        return super().update(**kwargs)


class RentalContract(models.Model):
    """
    חוזה שכירות — one issued version of a tenancy's contract, frozen.

    What the contract says (terms), the PDF it was drawn as (pdf), the
    fingerprints of both, its version, its tenancy and when it was issued are
    fixed the moment it is saved, and save() refuses to change them (who
    issued it may only be cleared, when that account is deleted). A signature (phase 3) is bound
    to terms_sha256, and a tenant who holds up the PDF they received years
    later must find it matching what is on file. So a contract is never edited:
    a mistake, or a change to the agreement, is corrected by issuing a new
    version, which voids the previous one unless it was signed. Only its life
    moves on — the status, and when and why it was voided.

    status: draft → sent → viewed → signed, or void. Phase 3 moves sent, viewed
    and signed; phase 2 issues drafts and voids them.

    The PDF is kept in the database: the host is serverless and has no file
    storage that outlives a request.
    """

    STATUS_DRAFT = 'draft'
    STATUS_SENT = 'sent'
    STATUS_VIEWED = 'viewed'
    STATUS_SIGNED = 'signed'
    STATUS_VOID = 'void'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'טיוטה'),
        (STATUS_SENT, 'נשלח'),
        (STATUS_VIEWED, 'נצפה'),
        (STATUS_SIGNED, 'נחתם'),
        (STATUS_VOID, 'בוטל'),
    ]
    # Still in play: may yet be sent, viewed or signed. A new version voids these.
    OPEN_STATUSES = (STATUS_DRAFT, STATUS_SENT, STATUS_VIEWED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PROTECT: a contract issued to a tenant is part of the tenancy's record;
    # the tenancy cannot be deleted from under it.
    tenancy = models.ForeignKey(
        Tenancy, on_delete=models.PROTECT, related_name='contracts', verbose_name='הסכם שכירות',
    )
    version = models.PositiveIntegerField(verbose_name='גרסה')
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT, verbose_name='סטטוס',
    )
    terms = models.JSONField(verbose_name='תנאי החוזה')
    terms_sha256 = models.CharField(max_length=64, editable=False, verbose_name='SHA-256 של התנאים')
    pdf = models.BinaryField(verbose_name='קובץ PDF')
    pdf_sha256 = models.CharField(max_length=64, editable=False, verbose_name='SHA-256 של הקובץ')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        verbose_name='הופק על ידי',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='תאריך הפקה')
    voided_at = models.DateTimeField(null=True, blank=True, verbose_name='תאריך ביטול')
    void_reason = models.TextField(blank=True, verbose_name='סיבת ביטול')

    objects = RentalContractQuerySet.as_manager()

    class Meta:
        db_table = 'rental_contracts'
        verbose_name = 'חוזה שכירות'
        verbose_name_plural = 'חוזי שכירות'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['tenancy', 'version'], name='rental_contract_version_per_tenancy'),
            models.CheckConstraint(check=models.Q(version__gte=1), name='rental_contract_version_from_one'),
            # The rules issue_contract keeps under its lock, held by the database
            # as well: one contract in play per tenancy (the open statuses,
            # OPEN_STATUSES), and never two signed.
            models.UniqueConstraint(
                fields=['tenancy'],
                condition=models.Q(status__in=['draft', 'sent', 'viewed']),
                name='rental_contract_one_open_per_tenancy',
            ),
            models.UniqueConstraint(
                fields=['tenancy'],
                condition=models.Q(status='signed'),
                name='rental_contract_one_signed_per_tenancy',
            ),
        ]

    def __str__(self):
        return f'{self.tenancy} · גרסה {self.version}'

    def save(self, *args, **kwargs):
        if self._state.adding:
            # The fingerprints are derived, never supplied: they describe
            # exactly the terms and the bytes this row stores.
            self.terms_sha256 = terms_sha256(self.terms)
            self.pdf_sha256 = sha256_hex(self.pdf)
        else:
            self._refuse_frozen_changes()
        super().save(*args, **kwargs)

    def _refuse_frozen_changes(self) -> None:
        # A deferred field was never loaded, so it cannot have been changed.
        loaded = [name for name in (*CONTRACT_FROZEN_FIELDS, 'created_by_id') if name in self.__dict__]
        if not loaded:
            return
        stored = type(self)._base_manager.filter(pk=self.pk).values(*loaded).first()
        if stored is None:
            return
        changed = {
            name: getattr(self, name) for name in loaded
            if _frozen_value(name, stored[name]) != _frozen_value(name, getattr(self, name))
        }
        changed = _refused_update_fields(changed)
        if changed:
            raise FrozenContractError(
                f'Rental contract {self.pk}: {", ".join(changed)} never change once issued; issue a new version'
            )

    def pdf_is_intact(self) -> bool:
        """The stored PDF still hashes to the fingerprint taken when it was issued."""
        return bool(self.pdf_sha256) and sha256_hex(self.pdf) == self.pdf_sha256
