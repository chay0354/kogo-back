import uuid
from django.db import models
from django.conf import settings


class City(models.Model):
    """ערים"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, verbose_name="שם העיר")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'cities'
        verbose_name = "עיר"
        verbose_name_plural = "ערים"
        ordering = ['name']

    def __str__(self):
        return self.name


class Branch(models.Model):
    """סניפים"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, verbose_name="שם הסניף")
    address = models.TextField(verbose_name="כתובת", blank=True)
    phone = models.CharField(max_length=20, verbose_name="טלפון", blank=True)
    email = models.EmailField(verbose_name="אימייל", blank=True)
    manager_name = models.CharField(max_length=200, verbose_name="שם מנהל", blank=True)
    city = models.ForeignKey(City, on_delete=models.SET_NULL, null=True, related_name='branches', verbose_name="עיר")
    
    # New fields for branches feature
    branch_codes = models.JSONField(default=list, blank=True, verbose_name="קודי סניף")
    cleaning_managers = models.JSONField(default=list, blank=True, verbose_name="אחראי ניקיון")
    cleaning_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name="עלות ניקיון")
    monthly_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name="עלות חודשית")
    wifi_name = models.CharField(max_length=100, blank=True, verbose_name="שם WiFi")
    wifi_code = models.CharField(max_length=100, blank=True, verbose_name="סיסמת WiFi")
    bluetooth_codes = models.JSONField(default=list, blank=True, verbose_name="קודי Bluetooth")
    custom_details = models.JSONField(default=list, blank=True, verbose_name="פרטים מותאמים אישית")
    
    is_active = models.BooleanField(default=True, verbose_name="פעיל")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'branches'
        verbose_name = "סניף"
        verbose_name_plural = "סניפים"
        ordering = ['name']

    def __str__(self):
        return self.name


class Room(models.Model):
    """חדרים"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='rooms', verbose_name="סניף")
    name = models.CharField(max_length=100, verbose_name="שם החדר")
    capacity = models.PositiveIntegerField(default=20, verbose_name="קיבולת")
    purpose = models.CharField(max_length=200, blank=True, verbose_name="ייעוד")
    notes = models.TextField(blank=True, verbose_name="הערות")
    is_active = models.BooleanField(default=True, verbose_name="פעיל")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'rooms'
        verbose_name = "חדר"
        verbose_name_plural = "חדרים"
        ordering = ['branch', 'name']

    def __str__(self):
        return f"{self.branch.name} - {self.name}"


class BranchFile(models.Model):
    """קבצים וסרטונים של סניפים"""
    FILE_TYPE_CHOICES = [
        ('video', 'וידאו'),
        ('document', 'מסמך'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='files', verbose_name="סניף")
    file_name = models.CharField(max_length=255, verbose_name="שם קובץ")
    file_type = models.CharField(max_length=20, choices=FILE_TYPE_CHOICES, verbose_name="סוג קובץ")
    file = models.FileField(upload_to='branch_files/', verbose_name="קובץ")
    file_size = models.IntegerField(verbose_name="גודל קובץ")
    mime_type = models.CharField(max_length=100, blank=True, verbose_name="סוג MIME")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'branch_files'
        verbose_name = "קובץ סניף"
        verbose_name_plural = "קבצי סניפים"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.branch.name} - {self.file_name}"


class UserProfile(models.Model):
    """
    Internal user profile for role-based access control.
    Roles:
    - manager: full access
    - worker: limited access (schedule-only in frontend; backend restricts management APIs)
    """

    ROLE_MANAGER = 'manager'
    ROLE_WORKER = 'worker'
    ROLE_CHOICES = [
        (ROLE_MANAGER, 'Manager'),
        (ROLE_WORKER, 'Worker'),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile',
        verbose_name="משתמש",
    )
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default=ROLE_WORKER,
        verbose_name="תפקיד",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'user_profiles'
        verbose_name = "פרופיל משתמש"
        verbose_name_plural = "פרופילי משתמשים"

    def __str__(self):
        return f"{self.user.email or self.user.username} ({self.role})"


class InstructorMonthlySnapshot(models.Model):
    """צילום חודשי של ביצועי מדריכים"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instructor = models.ForeignKey('instructors.Instructor', on_delete=models.CASCADE, related_name='monthly_snapshots', verbose_name="מדריך")
    month = models.CharField(max_length=7, verbose_name="חודש")  # YYYY-MM format
    total_lessons = models.PositiveIntegerField(default=0, verbose_name="סה״כ שיעורים")
    total_students = models.PositiveIntegerField(default=0, verbose_name="סה״כ תלמידים")
    base_revenue = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="הכנסות תיאורטיות", help_text="Revenue before discounts (lesson price × students)")
    total_discounts = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="סה״כ הנחות")
    total_revenue = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="סה״כ הכנסות", help_text="Actual collected revenue (from completed payments)")
    total_salary = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="סה״כ שכר")
    total_bonuses = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="סה״כ בונוסים")
    profit = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="רווח")
    cancelled_count = models.PositiveIntegerField(default=0, verbose_name="שיעורים שבוטלו")
    avg_attendance_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0, verbose_name="אחוז נוכחות ממוצע")
    
    # Salary history fields
    lesson_count = models.PositiveIntegerField(default=0, verbose_name="מספר שיעורים שהתרחשו")
    payment_per_lesson = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name="תשלום לשיעור (צילום)")
    is_finalized = models.BooleanField(default=False, verbose_name="חודש סופי")
    calculated_at = models.DateTimeField(auto_now=True, verbose_name="חושב בתאריך")
    
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'instructor_monthly_snapshots'
        verbose_name = "צילום חודשי - מדריך"
        verbose_name_plural = "צילומים חודשיים - מדריכים"
        unique_together = ['instructor', 'month']
        ordering = ['-month', 'instructor']

    def __str__(self):
        return f"{self.instructor.full_name} - {self.month}"


class LessonMonthlySnapshot(models.Model):
    """צילום חודשי של רווחיות שיעורים"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lesson = models.ForeignKey('courses.Lesson', on_delete=models.CASCADE, related_name='monthly_snapshots', verbose_name="שיעור")
    instructor = models.ForeignKey('instructors.Instructor', on_delete=models.CASCADE, related_name='lesson_snapshots', verbose_name="מדריך")
    course = models.ForeignKey('courses.Course', on_delete=models.CASCADE, related_name='lesson_snapshots', verbose_name="חוג")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='lesson_snapshots', verbose_name="סניף")
    month = models.CharField(max_length=7, verbose_name="חודש")  # YYYY-MM format
    enrolled_students = models.PositiveIntegerField(default=0, verbose_name="תלמידים רשומים")
    base_revenue = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="הכנסות תיאורטיות", help_text="Revenue before discounts")
    total_discounts = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="סה״כ הנחות")
    revenue = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="הכנסות", help_text="Actual collected revenue (from completed payments)")
    instructor_salary = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="שכר מדריך")
    profit = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="רווח")
    is_finalized = models.BooleanField(default=False, verbose_name="חודש סופי")
    calculated_at = models.DateTimeField(auto_now=True, verbose_name="חושב בתאריך")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'lesson_monthly_snapshots'
        verbose_name = "צילום חודשי - שיעור"
        verbose_name_plural = "צילומים חודשיים - שיעורים"
        unique_together = ['lesson', 'month']
        ordering = ['-month', 'lesson']

    def __str__(self):
        return f"{self.lesson} - {self.month}"


class BranchMonthlySnapshot(models.Model):
    """צילום חודשי של ביצועי סניפים"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='monthly_snapshots', verbose_name="סניף")
    month = models.CharField(max_length=7, verbose_name="חודש")  # YYYY-MM format
    total_students = models.PositiveIntegerField(default=0, verbose_name="סה״כ תלמידים")
    base_revenue = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="הכנסות תיאורטיות", help_text="Revenue before discounts")
    total_discounts = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="סה״כ הנחות")
    total_revenue = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="סה״כ הכנסות", help_text="Actual collected revenue (from completed payments)")
    
    # Expense breakdown (new fields for transparency)
    instructor_salaries = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="שכר מדריכים")
    instructor_bonuses = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="בונוסים למדריכים")
    operational_costs = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="הוצאות תפעוליות")
    
    # Total costs (sum of all expense components)
    instructor_costs = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="סה״כ הוצאות")
    
    profit = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name="רווח")
    active_courses_count = models.PositiveIntegerField(default=0, verbose_name="חוגים פעילים")
    is_finalized = models.BooleanField(default=False, verbose_name="חודש סופי")
    calculated_at = models.DateTimeField(auto_now=True, verbose_name="חושב בתאריך")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'branch_monthly_snapshots'
        verbose_name = "צילום חודשי - סניף"
        verbose_name_plural = "צילומים חודשיים - סניפים"
        unique_together = ['branch', 'month']
        ordering = ['-month', 'branch']

    def __str__(self):
        return f"{self.branch.name} - {self.month}"

