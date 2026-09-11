"""
Every signature a customer gives, kept as the evidence of what they agreed to.

A row is written once, at the moment of signing, and is never rewritten. It
holds the document exactly as it read then — the registration terms are one
editable row, so without a copy there would be no way to show later what a
parent agreed to — with its hash, the image of the signature and where the
request came from.

Registration terms signed in the widget are the first kind. Rental contracts
signed by merchants will be the second, which is why `kind` exists and why the
signer may be a business customer instead of a family.
"""
import uuid

from django.db import models

from apps.core.models import Branch
from apps.customers.models import BusinessCustomer, Child, Family


class SignatureImmutableError(Exception):
    """Raised on any attempt to rewrite a stored signature."""


class Signature(models.Model):
    KIND_REGISTRATION_TERMS = 'registration_terms'
    KIND_RENTAL_CONTRACT = 'rental_contract'
    KIND_CHOICES = [
        (KIND_REGISTRATION_TERMS, 'תקנון הרשמה'),
        (KIND_RENTAL_CONTRACT, 'חוזה שכירות'),
    ]

    SOURCE_WIDGET = 'widget'
    # A rental contract signed by the tenant through its short link (apps/rentals/signing.py).
    SOURCE_SIGNING_LINK = 'signing_link'
    SOURCE_CHOICES = [
        (SOURCE_WIDGET, "ווידג'ט הרשמה"),
        (SOURCE_SIGNING_LINK, 'קישור לחתימה'),
    ]

    # The only field that may change after signing. A parent who registers two
    # children signs once, but the widget sends one registration per child; the
    # second registration adds its child (through the M2M, which save() never
    # sees) and its payment and lesson ids here. What was signed never changes.
    MUTABLE_FIELDS = frozenset({'refs'})

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=40, choices=KIND_CHOICES, db_index=True, verbose_name="סוג")
    signed_at = models.DateTimeField(db_index=True, verbose_name="מועד החתימה")

    signer_name = models.CharField(max_length=200, verbose_name="שם החותם")
    signer_id_number = models.CharField(max_length=20, blank=True, verbose_name="ת.ז. החותם")
    signer_phone = models.CharField(max_length=20, blank=True, verbose_name="טלפון החותם")
    signer_email = models.EmailField(blank=True, verbose_name="אימייל החותם")

    # SET_NULL throughout: deleting a customer or a branch must not delete the
    # evidence of what they signed. The signer's own details are copied above
    # so the row still says who signed after the family is gone.
    family = models.ForeignKey(
        Family, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='signatures', verbose_name="משפחה",
    )
    children = models.ManyToManyField(
        Child, related_name='signatures', blank=True, verbose_name="ילדים",
    )
    business_customer = models.ForeignKey(
        BusinessCustomer, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='signatures', verbose_name="לקוח עסקי",
    )
    # The branch the registration was for, else the family's. Partners are
    # scoped by it (or by the family's branch).
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='signatures', verbose_name="סניף",
    )

    document_title = models.CharField(max_length=200, verbose_name="כותרת המסמך")
    document_html = models.TextField(verbose_name="נוסח המסמך כפי שנחתם")
    document_sha256 = models.CharField(max_length=64, verbose_name="SHA-256 של המסמך")
    consents = models.JSONField(default=dict, blank=True, verbose_name="הסכמות")

    signature_png = models.BinaryField(verbose_name="תמונת החתימה (PNG)")
    signature_sha256 = models.CharField(max_length=64, db_index=True, verbose_name="SHA-256 של החתימה")

    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name="כתובת IP")
    user_agent = models.CharField(max_length=500, blank=True, verbose_name="דפדפן")
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default=SOURCE_WIDGET, verbose_name="מקור",
    )
    refs = models.JSONField(default=dict, blank=True, verbose_name="הפניות")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")

    class Meta:
        db_table = 'signatures'
        verbose_name = "חתימה"
        verbose_name_plural = "חתימות"
        ordering = ['-signed_at', '-created_at']

    def __str__(self):
        return f"{self.get_kind_display()} — {self.signer_name} — {self.signed_at:%d/%m/%Y}"

    def save(self, *args, **kwargs):
        # A signature is the evidence of what was agreed. If the text, the
        # signer, the image or the moment could be edited afterwards, the row
        # would prove nothing — so once stored it is written again only to
        # append refs, and only through update_fields naming nothing else.
        if not self._state.adding:
            update_fields = kwargs.get('update_fields')
            if update_fields is None or not set(update_fields) <= self.MUTABLE_FIELDS:
                raise SignatureImmutableError(
                    'A stored signature cannot be changed; only refs may be appended '
                    '(save(update_fields=["refs"])).'
                )
        super().save(*args, **kwargs)
