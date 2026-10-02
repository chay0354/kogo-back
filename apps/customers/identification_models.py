"""
What the registration form's identification leaves behind.

`WidgetIdentifyAttempt` is every answer the form was given — it is both the
log the office can read and the memory the limits count from (the server is
serverless, so a counter kept in memory would be per instance and forgotten).

`FamilyIdentificationSwitch` is the history of the office switching a family's
identification off or back on, each time with its reason.
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.customers.models import Family


class WidgetIdentifyAttempt(models.Model):
    """One answer to "is this parent already with us?"."""

    OUTCOME_KNOWN = 'known'
    OUTCOME_KNOWN_NEAR = 'known_near'
    OUTCOME_NEAR = 'near'
    OUTCOME_UNKNOWN = 'unknown'
    OUTCOME_MISMATCH = 'mismatch'
    OUTCOME_OLD = 'old'
    OUTCOME_NO_CONSENT = 'no_consent'
    OUTCOME_HIDDEN = 'hidden'
    OUTCOME_DUPLICATE = 'duplicate'
    OUTCOME_LOCKED = 'locked'
    OUTCOME_DEVICE = 'device'
    OUTCOME_DEVICE_HELD = 'device_held'
    OUTCOME_NETWORK = 'network'
    OUTCOME_CAP = 'cap'
    OUTCOME_BOT = 'bot'
    OUTCOME_CHOICES = [
        (OUTCOME_KNOWN, 'זוהה'),
        (OUTCOME_KNOWN_NEAR, 'זוהה אחרי תיקון טלפון'),
        (OUTCOME_NEAR, 'טלפון דומה — הוצע תיקון'),
        (OUTCOME_UNKNOWN, 'לא מוכר'),
        (OUTCOME_MISMATCH, 'ת.ז. מוכרת, טלפון אחר'),
        (OUTCOME_OLD, 'לקוח ישן'),
        (OUTCOME_NO_CONSENT, 'בלי הסכמה בתקנון'),
        (OUTCOME_HIDDEN, 'כובה במשרד'),
        (OUTCOME_DUPLICATE, 'שתי משפחות עם אותה ת.ז.'),
        (OUTCOME_LOCKED, 'ת.ז. ננעלה אחרי ניסיונות שגויים'),
        (OUTCOME_DEVICE, 'יותר מדי הורים מאותו מכשיר'),
        (OUTCOME_DEVICE_HELD, 'מכשיר שנחסם היום'),
        (OUTCOME_NETWORK, 'יותר מדי הורים מאותה רשת'),
        (OUTCOME_CAP, 'נעצר: יותר מדי זיהויים בשעה'),
        (OUTCOME_BOT, 'נראה כמו רובוט'),
    ]
    # The answers that showed something of a family.
    REVEALING = (OUTCOME_KNOWN, OUTCOME_KNOWN_NEAR, OUTCOME_NEAR)
    # A wrong phone for an identity number that is on a family card.
    WRONG_PHONE = (OUTCOME_MISMATCH, OUTCOME_NEAR)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    # A keyed hash of the identity number typed: enough to count tries on it,
    # without keeping the numbers of people who are not customers.
    id_hash = models.CharField(max_length=64, db_index=True)
    family = models.ForeignKey(
        Family, on_delete=models.SET_NULL, null=True, blank=True, related_name='identify_attempts',
    )
    # A random id the form keeps in the browser, and a keyed hash of the address.
    device_id = models.CharField(max_length=64, blank=True, db_index=True)
    ip_hash = models.CharField(max_length=64, blank=True, db_index=True)
    outcome = models.CharField(max_length=20, choices=OUTCOME_CHOICES, db_index=True)
    notice_sent = models.BooleanField(default=False)

    class Meta:
        db_table = 'widget_identify_attempts'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.created_at:%d/%m/%Y %H:%M} {self.outcome}'


class FamilyIdentificationSwitch(models.Model):
    """The office switched a family's identification off, or back on."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name='identification_switches')
    blocked = models.BooleanField(verbose_name="הזיהוי כובה")
    reason = models.TextField(verbose_name="סיבה")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='family_identification_switches',
    )
    changed_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = 'family_identification_switches'
        ordering = ['-changed_at']

    def __str__(self):
        return f'{self.family_id} {"off" if self.blocked else "on"} {self.changed_at:%d/%m/%Y}'


class WidgetRateBucket(models.Model):
    """
    How many times one address asked one of the form's open endpoints in one hour.

    The server keeps no memory between requests, so a limit that holds has to
    be counted here. The address is kept as a keyed hash.
    """

    scope = models.CharField(max_length=30)
    key_hash = models.CharField(max_length=64)
    bucket_start = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'widget_rate_buckets'
        unique_together = [('scope', 'key_hash', 'bucket_start')]
        indexes = [models.Index(fields=['bucket_start'])]
