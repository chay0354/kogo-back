import uuid
from django.db import models
from django.conf import settings
from apps.core.models import Branch, Room, City
from apps.customers.models import Child
from apps.courses.models import Lesson


class ScheduleEvent(models.Model):
    """
    אירועים בלוח שנה

    Regular events are NOT included in snapshot profit/revenue.
    Studio rentals (is_studio_rental) contribute price_per_session per occurrence
    in dashboard financial aggregates for the selected date range.
    """
    EVENT_TYPE_CHOICES = [
        ('one_time', 'חד פעמי'),
        ('weekly', 'שבועי'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, verbose_name="שם אירוע")
    event_date = models.DateField(verbose_name="תאריך")
    start_time = models.TimeField(null=True, blank=True, verbose_name="שעת התחלה")
    end_time = models.TimeField(null=True, blank=True, verbose_name="שעת סיום")
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES, default='one_time', verbose_name="סוג")
    is_daily_event = models.BooleanField(default=False, verbose_name="אירוע יומי")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='schedule_events', verbose_name="סניף")
    studio = models.ForeignKey(Room, on_delete=models.SET_NULL, null=True, blank=True, related_name='schedule_events', verbose_name="סטודיו")
    city = models.ForeignKey(City, on_delete=models.SET_NULL, null=True, blank=True, related_name='schedule_events', verbose_name="עיר")
    location = models.CharField(max_length=200, blank=True, verbose_name="מיקום")  # Deprecated: use city instead
    color = models.CharField(max_length=7, blank=True, default='#9333ea', verbose_name="צבע")  # hex color, default purple
    notes = models.TextField(blank=True, verbose_name="הערות")
    files = models.JSONField(default=list, blank=True, verbose_name="קבצים מצורפים")  # Array of file URLs
    # 0=Sunday .. 6=Saturday (same as Lesson.day_of_week). Empty => single weekday from event_date anchor.
    weekly_repeat_days = models.JSONField(
        default=list,
        blank=True,
        verbose_name="ימי חזרה שבועית",
    )
    assigned_instructors = models.ManyToManyField('instructors.Instructor', blank=True, related_name='assigned_events', verbose_name="מדריכים משויכים")
    is_active = models.BooleanField(default=True, verbose_name="פעיל")
    is_studio_rental = models.BooleanField(
        default=False,
        verbose_name="שכירות סטודיו",
        help_text="When true, price_per_session is counted as revenue per occurrence in dashboards.",
    )
    renter_name = models.CharField(max_length=200, blank=True, verbose_name="שם השוכר")
    price_per_session = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name="מחיר למופע",
        help_text="Revenue per rental occurrence (one_time = once; weekly = each week in range).",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'schedule_events'
        verbose_name = "אירוע"
        verbose_name_plural = "אירועים"
        ordering = ['event_date', 'start_time']

    def __str__(self):
        return f"{self.name} - {self.event_date.strftime('%d/%m/%Y')}"
    
    @property
    def is_event(self):
        """Property to identify this as an event (vs a lesson)"""
        return True


class SubscriptionLog(models.Model):
    """לוג מנויים"""
    ACTION_TYPE_CHOICES = [
        ('renew', 'חידוש'),
        ('cancel', 'ביטול'),
        ('expire', 'פג תוקף'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    child = models.ForeignKey(Child, on_delete=models.CASCADE, related_name='subscription_logs', verbose_name="ילד")
    action_type = models.CharField(max_length=20, choices=ACTION_TYPE_CHOICES, verbose_name="סוג פעולה")
    previous_status = models.CharField(max_length=50, blank=True, verbose_name="סטטוס קודם")
    new_status = models.CharField(max_length=50, blank=True, verbose_name="סטטוס חדש")
    previous_end_date = models.DateField(null=True, blank=True, verbose_name="תאריך סיום קודם")
    new_end_date = models.DateField(null=True, blank=True, verbose_name="תאריך סיום חדש")
    performed_by = models.CharField(max_length=200, blank=True, verbose_name="מבצע")
    reason = models.TextField(blank=True, verbose_name="סיבה")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")

    class Meta:
        db_table = 'subscription_logs'
        verbose_name = "לוג מנוי"
        verbose_name_plural = "לוגים מנויים"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.child.full_name} - {self.get_action_type_display()}"


class SubscriptionReminder(models.Model):
    """תזכורות מנוי"""
    STATUS_CHOICES = [
        ('pending', 'ממתין'),
        ('sent', 'נשלח'),
        ('failed', 'נכשל'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    child = models.ForeignKey(Child, on_delete=models.CASCADE, related_name='subscription_reminders', verbose_name="ילד")
    reminder_type = models.CharField(max_length=100, verbose_name="סוג תזכורת")
    days_before_end = models.PositiveIntegerField(verbose_name="ימים לפני סיום")
    phone_number = models.CharField(max_length=20, verbose_name="טלפון")
    message_content = models.TextField(verbose_name="תוכן הודעה")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="סטטוס")
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name="נשלח בתאריך")

    class Meta:
        db_table = 'subscription_reminders'
        verbose_name = "תזכורת מנוי"
        verbose_name_plural = "תזכורות מנויים"
        ordering = ['-sent_at']

    def __str__(self):
        return f"{self.child.full_name} - {self.reminder_type}"


class LessonCancellation(models.Model):
    """
    Date-specific cancellation for recurring lessons.
    (A recurring lesson represents a weekly slot; cancellations apply to a specific occurrence date.)
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='cancellations', verbose_name="שיעור")
    occurrence_date = models.DateField(verbose_name="תאריך מופע")
    reason = models.TextField(blank=True, verbose_name="סיבה")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='lesson_cancellations',
        verbose_name="בוטל על ידי",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")

    class Meta:
        db_table = 'lesson_cancellations'
        verbose_name = "ביטול שיעור (תאריך ספציפי)"
        verbose_name_plural = "ביטולי שיעורים (תאריך ספציפי)"
        unique_together = ['lesson', 'occurrence_date']
        ordering = ['-occurrence_date', '-created_at']

    def __str__(self):
        return f"{self.lesson} @ {self.occurrence_date}"

