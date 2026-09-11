"""
Payment links: a shareable page where anyone pays a fixed amount by card.

The owner creates a link for a purpose (a year-end show, a camp, a shirt),
tags it with the business and category the money belongs to, and gives it
one or more price options. Anyone with the link picks an option, enters a
new card on Tranzila's hosted page, and the payment lands in the CRM under
that tag. One link serves many payers.

Money rails: a PaymentLinkPayment row is created *before* the payer is sent
to Tranzila, with the amount locked on it. The callback never trusts the
posted sum — it compares Tranzila's sum with the locked amount and marks
the row completed only when they match; otherwise it goes to review.
"""
from __future__ import annotations

import secrets
import string
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


def _new_slug() -> str:
    return secrets.token_urlsafe(9)


class PaymentLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.CharField(max_length=24, unique=True, default=_new_slug, editable=False)
    title = models.CharField(max_length=120, verbose_name='כותרת')
    description = models.TextField(blank=True, verbose_name='תיאור לעמוד התשלום')
    business = models.ForeignKey(
        'core.Business', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='payment_links', verbose_name='עסק',
    )
    business_category = models.ForeignKey(
        'core.BusinessCategory', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='payment_links', verbose_name='קטגוריה בעסק',
    )
    branch = models.ForeignKey(
        'core.Branch', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='payment_links', verbose_name='סניף',
    )
    is_active = models.BooleanField(default=True, verbose_name='פעיל')
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name='תוקף עד')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='payment_links_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payment_links'
        ordering = ['-created_at']
        verbose_name = 'קישור תשלום'
        verbose_name_plural = 'קישורי תשלום'

    def __str__(self) -> str:
        return self.title

    def is_open(self) -> bool:
        if not self.is_active:
            return False
        if self.expires_at and self.expires_at <= timezone.now():
            return False
        return True

    def public_url(self) -> str:
        from apps.core.password_reset_email import crm_frontend_url
        return f'{crm_frontend_url()}/pay/{self.slug}'

    def save(self, *args, **kwargs):
        # token_urlsafe(9) has 72 bits; a clash is theoretical, but retry anyway.
        for _ in range(5):
            try:
                return super().save(*args, **kwargs)
            except Exception as exc:  # IntegrityError on slug
                if 'slug' not in str(exc) or self._state.adding is False:
                    raise
                self.slug = _new_slug()
        return super().save(*args, **kwargs)


class PaymentLinkOption(models.Model):
    """One price the payer can pick. Never deleted — payments point at it."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    link = models.ForeignKey(PaymentLink, on_delete=models.CASCADE, related_name='options')
    label = models.CharField(max_length=120, verbose_name='שם האפשרות')
    amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='סכום')
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'payment_link_options'
        ordering = ['sort_order', 'created_at']

    def __str__(self) -> str:
        return f'{self.label} — ₪{self.amount}'


class PaymentLinkPayment(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'
    STATUS_REVIEW = 'review'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'ממתין'),
        (STATUS_COMPLETED, 'הושלם'),
        (STATUS_FAILED, 'נכשל'),
        (STATUS_REVIEW, 'לבדיקה'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    link = models.ForeignKey(PaymentLink, on_delete=models.PROTECT, related_name='payments')
    option = models.ForeignKey(PaymentLinkOption, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    option_label = models.CharField(max_length=120, blank=True)
    # Locked when the row is created; the callback is checked against it.
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payer_name = models.CharField(max_length=120)
    payer_phone = models.CharField(max_length=30, blank=True)
    payer_email = models.EmailField(blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)
    tranzila_transaction = models.ForeignKey(
        'customers.TranzilaTransaction', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='payment_link_payments',
    )
    gateway_transaction_id = models.CharField(max_length=100, blank=True)
    gateway_confirmation_code = models.CharField(max_length=100, blank=True)
    card_last4 = models.CharField(max_length=4, blank=True)
    card_type = models.CharField(max_length=30, blank=True)
    # What Tranzila said it charged — kept when it disagrees with `amount`.
    reported_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    failure_reason = models.TextField(blank=True)
    failure_code = models.CharField(max_length=10, blank=True)
    review_reason = models.CharField(max_length=200, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    # Set by hand when the office issues a document for this payment, so the
    # income report stops listing it as undocumented.
    formal_document = models.ForeignKey(
        'documents.FormalDocument', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='payment_link_payments',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payment_link_payments'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['link', 'status']),
            models.Index(fields=['ip_address', 'created_at']),
            models.Index(fields=['payer_phone', 'created_at']),
        ]

    def __str__(self) -> str:
        return f'{self.payer_name} — ₪{self.amount} ({self.status})'

    @property
    def is_final(self) -> bool:
        return self.status in (self.STATUS_COMPLETED, self.STATUS_REVIEW)


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal('0.01'))


_TOKEN_ALPHABET = string.ascii_letters + string.digits


def new_card_link_token() -> str:
    """
    10 base-62 characters (~59 bits): short enough to read off a phone, far too
    wide to guess — and the preview endpoint is throttled on top of that.
    """
    while True:
        token = ''.join(secrets.choice(_TOKEN_ALPHABET) for _ in range(10))
        if not CardLink.objects.filter(token=token).exists():
            return token


class CardLink(models.Model):
    """
    A link the office sends to an existing customer to enter card details.

    Two kinds: a standing order for a lesson (the card becomes the monthly
    token, the first charge follows the same pricing as a widget signup), or
    a one-time charge for a fixed amount tagged by business/category.

    ``token_version`` goes into the signed token, so cancelling a link (or
    issuing a new one) invalidates the old URL without touching updated_at.
    """
    KIND_STANDING_ORDER = 'standing_order'
    KIND_ONE_TIME = 'one_time'
    KIND_CHOICES = [(KIND_STANDING_ORDER, 'הוראת קבע'), (KIND_ONE_TIME, 'חיוב חד-פעמי')]

    STATUS_PENDING = 'pending'
    STATUS_PROCESSING = 'processing'
    STATUS_COMPLETED = 'completed'
    STATUS_REVIEW = 'review'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'ממתין'),
        (STATUS_PROCESSING, 'בעיבוד'),
        (STATUS_COMPLETED, 'הושלם'),
        (STATUS_REVIEW, 'לבדיקה'),
        (STATUS_CANCELLED, 'בוטל'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)
    child = models.ForeignKey('customers.Child', on_delete=models.CASCADE, related_name='card_links')
    # standing_order: the lesson the order bills (the cron skips lesson-less orders).
    lesson = models.ForeignKey('courses.Lesson', on_delete=models.SET_NULL, null=True, blank=True, related_name='card_links')
    # A twice/thrice-a-week track. Priced at its combined price exactly as the
    # widget prices it; `lesson` is then the member day the standing order hangs
    # on, and every other day is enrolled alongside it.
    bundle = models.ForeignKey('courses.LessonBundle', on_delete=models.SET_NULL, null=True, blank=True, related_name='card_links')
    include_registration_fee = models.BooleanField(default=True)
    # one_time
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    description = models.CharField(max_length=200, blank=True)
    branch = models.ForeignKey('core.Branch', on_delete=models.SET_NULL, null=True, blank=True, related_name='card_links')
    business = models.ForeignKey('core.Business', on_delete=models.SET_NULL, null=True, blank=True, related_name='card_links')
    business_category = models.ForeignKey('core.BusinessCategory', on_delete=models.SET_NULL, null=True, blank=True, related_name='card_links')
    token_version = models.PositiveIntegerField(default=1)
    # The public token in the URL. Short on purpose: this link is read off a
    # WhatsApp message by a parent, and a 119-character signed blob made the
    # message look like spam. Regenerated whenever token_version moves, which is
    # what retires an old URL.
    #
    # Nullable on purpose. Vercel runs `migrate` at build time, so this column can
    # exist while the previous code is still serving — and that code inserts card
    # links without it. NULL keeps those inserts valid (Postgres allows many NULLs
    # under a unique index); the first read of such a link fills a token in.
    token = models.CharField(max_length=32, unique=True, null=True, blank=True)
    charge_started_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    review_reason = models.CharField(max_length=200, blank=True)
    payment = models.OneToOneField('customers.Payment', on_delete=models.SET_NULL, null=True, blank=True, related_name='card_link')
    recurring_payment = models.ForeignKey('customers.RecurringPayment', on_delete=models.SET_NULL, null=True, blank=True, related_name='card_links')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='card_links_created')
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_result = models.JSONField(default=dict, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'card_links'
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = new_card_link_token()
        super().save(*args, **kwargs)

    def rotate_token(self) -> str:
        """A new URL for this link; the previous one stops resolving."""
        self.token = new_card_link_token()
        self.token_version += 1
        return self.token

    def __str__(self) -> str:
        return f'{self.get_kind_display()} — {self.child_id} ({self.status})'
