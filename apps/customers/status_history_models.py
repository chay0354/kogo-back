"""
Child Status History Models
Tracks status changes over time for quit percentage calculations
"""
import uuid
from django.db import models
from django.utils import timezone
from apps.customers.models import Child


class ChildStatusHistory(models.Model):
    """
    היסטוריית שינויי סטטוס של ילדים
    
    Tracks all status changes for children to enable:
    - Quit percentage calculations
    - Status change analysis over time
    - Churn analysis
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    child = models.ForeignKey(
        Child,
        on_delete=models.CASCADE,
        related_name='status_history',
        verbose_name="ילד"
    )
    
    # Status change details
    previous_status = models.CharField(
        max_length=20,
        verbose_name="סטטוס קודם",
        help_text="הסטטוס לפני השינוי"
    )
    new_status = models.CharField(
        max_length=20,
        verbose_name="סטטוס חדש",
        help_text="הסטטוס אחרי השינוי"
    )
    
    # Timestamp
    changed_at = models.DateTimeField(
        default=timezone.now,
        verbose_name="תאריך שינוי",
        db_index=True
    )
    
    # Optional metadata
    reason = models.TextField(
        blank=True,
        verbose_name="סיבה",
        help_text="סיבה לשינוי הסטטוס (אופציונלי)"
    )
    changed_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="שונה על ידי"
    )
    
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    
    class Meta:
        db_table = 'child_status_history'
        verbose_name = "היסטוריית סטטוס ילד"
        verbose_name_plural = "היסטוריית סטטוסים של ילדים"
        ordering = ['-changed_at']
        indexes = [
            models.Index(fields=['child', '-changed_at']),
            models.Index(fields=['previous_status', 'new_status']),
            models.Index(fields=['changed_at']),
        ]
    
    def __str__(self):
        return f"{self.child.full_name}: {self.previous_status} → {self.new_status} ({self.changed_at.date()})"
