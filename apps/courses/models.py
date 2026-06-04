import uuid
from django.conf import settings
from django.db import models
from apps.core.models import Branch, Room
from apps.instructors.models import Instructor


class CourseType(models.Model):
    """תחומים - Course categories (e.g., Capoeira, Basketball, Judo)"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, verbose_name="שם התחום")
    description = models.TextField(blank=True, verbose_name="תיאור")
    is_active = models.BooleanField(default=True, verbose_name="פעיל")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'course_types'
        verbose_name = "תחום"
        verbose_name_plural = "תחומים"
        ordering = ['name']

    def __str__(self):
        return self.name


class Course(models.Model):
    """חוגים - Specific courses within a course type (e.g., Beginners, Advanced)"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course_type = models.ForeignKey(CourseType, on_delete=models.CASCADE, related_name='courses', null=True, blank=True, verbose_name="תחום")
    name = models.CharField(max_length=200, verbose_name="שם החוג")
    description = models.TextField(blank=True, verbose_name="תיאור")
    price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="מחיר")
    capacity = models.PositiveIntegerField(verbose_name="קיבולת")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='courses', verbose_name="סניף")
    min_age = models.PositiveIntegerField(null=True, blank=True, verbose_name="גיל מינימום")
    max_age = models.PositiveIntegerField(null=True, blank=True, verbose_name="גיל מקסימום")
    is_active = models.BooleanField(default=True, verbose_name="פעיל")
    managers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='assigned_courses',
        blank=True,
        verbose_name="מנהלים מורשים",
        help_text="מנהלים שיכולים לראות ולנהל חוג זה. ריק = אף מנהל (מלבד מנהל-על).",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'courses'
        verbose_name = "חוג"
        verbose_name_plural = "חוגים"
        ordering = ['course_type', 'name']

    def __str__(self):
        return f"{self.course_type.name} - {self.name}"


class Lesson(models.Model):
    """שיעורים"""
    STATUS_CHOICES = [
        ('scheduled', 'מתוכנן'),
        ('completed', 'הושלם'),
        ('cancelled', 'בוטל'),
    ]
    
    DAY_OF_WEEK_CHOICES = [
        (0, 'ראשון'),
        (1, 'שני'),
        (2, 'שלישי'),
        (3, 'רביעי'),
        (4, 'חמישי'),
        (5, 'שישי'),
        (6, 'שבת'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='lessons', verbose_name="חוג")
    room = models.ForeignKey(Room, on_delete=models.SET_NULL, null=True, blank=True, related_name='lessons', verbose_name="חדר")
    instructor = models.ForeignKey(Instructor, on_delete=models.SET_NULL, null=True, related_name='lessons', verbose_name="מדריך")
    day_of_week = models.IntegerField(choices=DAY_OF_WEEK_CHOICES, verbose_name="יום בשבוע")
    start_time = models.TimeField(verbose_name="שעת התחלה")
    end_time = models.TimeField(verbose_name="שעת סיום")
    lesson_date = models.DateField(null=True, blank=True, verbose_name="תאריך שיעור")
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name="מחיר שיעור")
    lesson_price_override = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name="מחיר מותאם")
    additional_course_prices = models.JSONField(
        default=list,
        blank=True,
        verbose_name="מחירים מדורגים לרישום מקביל",
        help_text=(
            "List of {course_index, price} entries used when a child is concurrently enrolled "
            "in N other courses. course_index is 1-based: 2 = student's 2nd course, 3 = 3rd, ... "
            "Used by get_lesson_price_for_course_index to pick a discounted/different price per tier."
        ),
    )
    instructor_salary_override = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name="שכר מדריך מותאם")
    is_recurring = models.BooleanField(default=True, verbose_name="חוזר שבועית")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled', verbose_name="סטטוס")
    cancellation_reason = models.TextField(null=True, blank=True, verbose_name="סיבת ביטול")
    cancelled_at = models.DateTimeField(null=True, blank=True, verbose_name="בוטל בתאריך")
    room_text = models.CharField(max_length=100, blank=True, verbose_name="חדר (טקסט)")  # deprecated field
    notes = models.TextField(blank=True, verbose_name="הערות")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'lessons'
        verbose_name = "שיעור"
        verbose_name_plural = "שיעורים"
        ordering = ['day_of_week', 'start_time']
        indexes = []

    def __str__(self):
        day_name = dict(self.DAY_OF_WEEK_CHOICES)[self.day_of_week]
        return f"{self.course.name} - {day_name} {self.start_time.strftime('%H:%M')}"
    
    def has_occurred(self):
        """Check if lesson occurred (for salary calculation)"""
        from django.utils import timezone
        return (
            self.status != 'cancelled' and 
            self.lesson_date and 
            self.lesson_date < timezone.now().date()
        )


