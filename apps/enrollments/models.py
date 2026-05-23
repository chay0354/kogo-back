import uuid
from django.db import models
from apps.courses.models import Course, Lesson
from apps.customers.models import Child


class Enrollment(models.Model):
    """
    רישום לחוגים (טבלה ישנה)
    
    DEPRECATED: This model is kept for backward compatibility only.
    New enrollments should use LessonEnrollment instead, which enrolls
    students in specific lessons rather than entire courses.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='enrollments', verbose_name="חוג")
    child = models.ForeignKey(Child, on_delete=models.CASCADE, related_name='enrollments', verbose_name="ילד")
    is_active = models.BooleanField(default=True, verbose_name="פעיל")
    enrolled_at = models.DateTimeField(auto_now_add=True, verbose_name="מועד רישום")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'enrollments'
        verbose_name = "רישום לחוג"
        verbose_name_plural = "רישומים לחוגים"
        unique_together = ['course', 'child']
        ordering = ['-enrolled_at']

    def __str__(self):
        return f"{self.child.full_name} - {self.course.name}"


class LessonEnrollment(models.Model):
    """רישום לשיעורים"""
    STATUS_CHOICES = [
        ('active', 'פעיל'),
        ('inactive', 'לא פעיל'),
        # Enrollment is kept in the lesson for scheduling/teaching load, but indicates a billing issue.
        ('payments_problem', 'בעיית תשלום'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='enrollments', verbose_name="שיעור")
    child = models.ForeignKey(Child, on_delete=models.CASCADE, related_name='lesson_enrollments', verbose_name="ילד")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active', verbose_name="סטטוס")
    start_date = models.DateField(null=True, blank=True, verbose_name="תאריך התחלה")
    end_date = models.DateField(null=True, blank=True, verbose_name="תאריך סיום")
    enrolled_at = models.DateTimeField(auto_now_add=True, verbose_name="מועד רישום")
    notes = models.TextField(blank=True, verbose_name="הערות")
    # Trial-reminder tracking — populated only for trial enrollments.
    trial_lesson_date = models.DateField(null=True, blank=True, verbose_name="תאריך שיעור ניסיון")
    trial_evening_reminder_sent_at = models.DateTimeField(null=True, blank=True, verbose_name="תזכורת ערב נשלחה")
    trial_followup_reminder_sent_at = models.DateTimeField(null=True, blank=True, verbose_name="תזכורת 72 שעות נשלחה")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'lesson_enrollments'
        verbose_name = "רישום לשיעור"
        verbose_name_plural = "רישומים לשיעורים"
        unique_together = ['lesson', 'child']
        ordering = ['-enrolled_at']
        indexes = [
            models.Index(fields=['lesson', 'status']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"{self.child.full_name} - {self.lesson}"


class LessonAttendance(models.Model):
    """נוכחות"""
    STATUS_CHOICES = [
        ('present', 'נוכח'),
        ('absent', 'נעדר'),
        ('not_marked', 'לא סומן'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='attendance_records', verbose_name="שיעור")
    child = models.ForeignKey(Child, on_delete=models.CASCADE, related_name='attendance_records', verbose_name="ילד")
    occurrence_date = models.DateField(null=True, blank=True, verbose_name="תאריך מופע")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='not_marked', verbose_name="סטטוס")
    notes = models.TextField(blank=True, verbose_name="הערות")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")

    class Meta:
        db_table = 'lesson_attendance'
        verbose_name = "נוכחות"
        verbose_name_plural = "נוכחות"
        unique_together = ['lesson', 'child', 'occurrence_date']
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.child.full_name} - {self.lesson} - {self.get_status_display()}"


class ChildAbsence(models.Model):
    """היעדרויות ילדים - מעקב אחר היעדרויות לצורך ניתוח"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    child = models.ForeignKey(Child, on_delete=models.CASCADE, related_name='absences', verbose_name="ילד")
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='child_absences', verbose_name="שיעור")
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='child_absences', verbose_name="חוג")
    occurrence_date = models.DateField(verbose_name="תאריך היעדרות")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")

    class Meta:
        db_table = 'child_absences'
        verbose_name = "היעדרות ילד"
        verbose_name_plural = "היעדרויות ילדים"
        unique_together = ['child', 'lesson', 'occurrence_date']
        ordering = ['-occurrence_date']
        indexes = [
            models.Index(fields=['child', 'occurrence_date']),
            models.Index(fields=['child', '-occurrence_date']),
        ]

    def __str__(self):
        return f"{self.child.full_name} - {self.lesson} - {self.occurrence_date}"

