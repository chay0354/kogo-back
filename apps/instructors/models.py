import uuid
from django.db import models
from apps.core.models import Branch


class Instructor(models.Model):
    """מדריכים"""
    SALARY_MODEL_CHOICES = [
        ('fixed_per_lesson', 'שכר קבוע לשיעור'),
        ('tiered_by_students', 'מדורג לפי תלמידים'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    first_name = models.CharField(max_length=100, verbose_name="שם פרטי")
    last_name = models.CharField(max_length=100, verbose_name="שם משפחה")
    phone = models.CharField(max_length=20, verbose_name="טלפון")
    email = models.EmailField(verbose_name="אימייל")
    specialization = models.CharField(max_length=200, blank=True, verbose_name="התמחות")
    primary_branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, related_name='primary_instructors', verbose_name="סניף ראשי")
    salary_model_type = models.CharField(max_length=50, choices=SALARY_MODEL_CHOICES, default='fixed_per_lesson', verbose_name="סוג מודל שכר")
    fixed_salary_per_lesson = models.DecimalField(max_digits=10, decimal_places=2, default=250, verbose_name="שכר קבוע לשיעור")
    is_active = models.BooleanField(default=True, verbose_name="פעיל")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'instructors'
        verbose_name = "מדריך"
        verbose_name_plural = "מדריכים"
        ordering = ['last_name', 'first_name']

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        """
        USAGE: Used in InstructorListSerializer
        USAGE: Used in Django admin displays
        USAGE: Referenced in ChildWithDetailsSerializer for instructor names
        """
        return f"{self.first_name} {self.last_name}"


class InstructorSalaryTier(models.Model):
    """מדרגות שכר מדריכים"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instructor = models.ForeignKey(Instructor, on_delete=models.CASCADE, related_name='salary_tiers', verbose_name="מדריך")
    min_students = models.PositiveIntegerField(verbose_name="מינימום תלמידים")
    max_students = models.PositiveIntegerField(null=True, blank=True, verbose_name="מקסימום תלמידים")
    salary_per_lesson = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="שכר לשיעור")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'instructor_salary_tiers'
        verbose_name = "מדרגת שכר"
        verbose_name_plural = "מדרגות שכר"
        ordering = ['instructor', 'min_students']

    def __str__(self):
        if self.max_students:
            return f"{self.instructor.full_name} - {self.min_students}-{self.max_students} תלמידים: ₪{self.salary_per_lesson}"
        return f"{self.instructor.full_name} - {self.min_students}+ תלמידים: ₪{self.salary_per_lesson}"


class InstructorBranch(models.Model):
    """שיוך מדריכים לסניפים"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instructor = models.ForeignKey(Instructor, on_delete=models.CASCADE, related_name='branch_assignments', verbose_name="מדריך")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='instructor_assignments', verbose_name="סניף")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")

    class Meta:
        db_table = 'instructor_branches'
        verbose_name = "שיוך מדריך לסניף"
        verbose_name_plural = "שיוכי מדריכים לסניפים"
        unique_together = ['instructor', 'branch']

    def __str__(self):
        return f"{self.instructor.full_name} - {self.branch.name}"


class InstructorBonus(models.Model):
    """בונוסים למדריכים"""
    BONUS_TYPE_CHOICES = [
        ('one_time', 'חד פעמי'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instructor = models.ForeignKey(Instructor, on_delete=models.CASCADE, related_name='bonuses', verbose_name="מדריך")
    bonus_type = models.CharField(max_length=20, choices=BONUS_TYPE_CHOICES, default='one_time', verbose_name="סוג בונוס")
    amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="סכום")
    bonus_date = models.DateField(verbose_name="תאריך בונוס")
    description = models.TextField(blank=True, verbose_name="תיאור")
    notes = models.TextField(blank=True, verbose_name="הערות")
    period_start = models.DateField(null=True, blank=True, verbose_name="תחילת תקופה")
    period_end = models.DateField(null=True, blank=True, verbose_name="סוף תקופה")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'instructor_bonuses'
        verbose_name = "בונוס"
        verbose_name_plural = "בונוסים"
        ordering = ['-bonus_date', '-created_at']

    def __str__(self):
        return f"{self.instructor.full_name} - ₪{self.amount} - {self.bonus_date.strftime('%m/%Y')}"

