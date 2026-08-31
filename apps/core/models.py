import uuid
from django.db import models
from django.conf import settings


class City(models.Model):
    """ערים"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True, verbose_name="שם העיר")
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

    is_external = models.BooleanField(default=False, verbose_name="סניף חיצוני")
    external_link = models.CharField(max_length=500, blank=True, verbose_name="לינק לסניף")
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
    - partner: full UI scoped to assigned branches (שותף)
    """

    ROLE_MANAGER = 'manager'
    ROLE_WORKER = 'worker'
    ROLE_PARTNER = 'partner'
    ROLE_CHOICES = [
        (ROLE_MANAGER, 'Manager'),
        (ROLE_WORKER, 'Worker'),
        (ROLE_PARTNER, 'Partner'),
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
    # How many times this account has signed in. Drives the guided tour:
    # the first sign-in shows it and cannot be skipped, the next two show it
    # with a skip, and from the fourth it no longer opens on its own.
    # Kept on the account rather than in the browser so clearing site data or
    # moving to another device does not restart the tour.
    login_count = models.PositiveIntegerField(default=0, verbose_name="מספר כניסות")
    tour_completed_at = models.DateTimeField(
        null=True, blank=True, verbose_name="סיים הדרכה",
        help_text="Set when the user finishes or dismisses the tour; stops it opening again.",
    )

    assigned_branches = models.ManyToManyField(
        Branch,
        blank=True,
        related_name='assigned_partners',
        verbose_name="סניפים משויכים",
        help_text="סניפים שהשותף רשאי לראות ולנהל",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'user_profiles'
        verbose_name = "פרופיל משתמש"
        verbose_name_plural = "פרופילי משתמשים"

    def __str__(self):
        return f"{self.user.email or self.user.username} ({self.role})"


class LinkedUserAccess(models.Model):
    """
    One account another account may look at.

    A head instructor who covers for colleagues needs to open their registers
    and see their numbers without a second login. A manager grants that from
    the users screen; the instructor then gets a switcher next to the branch
    picker.

    One-way: a link lets `owner` reach `linked`'s lessons, never the reverse.
    Within those lessons the owner can do what an instructor does — read the
    register and mark attendance — because covering a colleague is the whole
    point. It grants nothing outside scheduling: no salary, no customer records,
    no management screens, and no ability to create further links.

    A colleague who teaches in several branches can be handed over one branch at
    a time: set `branch` and the link reaches that branch only. Leave it empty
    and the link reaches the colleague's whole timetable.

    Every request re-checks the row. The id in the query string is a request,
    never a permission.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='linked_accounts',
        verbose_name="משתמש",
        help_text="המשתמש שמקבל את ההרשאה לצפות",
    )
    linked = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='linked_from',
        verbose_name="משתמש מקושר",
        help_text="המשתמש שאותו מותר לצפות",
    )
    branch = models.ForeignKey(
        Branch,
        # Deleting a branch takes the limited link with it. SET_NULL would turn
        # a one-branch link into a whole-timetable one without anyone asking.
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='linked_user_access',
        verbose_name="סניף",
        help_text="השאירו ריק כדי לצפות בכל הסניפים של המשתמש המקושר",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='linked_users_created',
        verbose_name="נוצר על ידי",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")

    class Meta:
        db_table = 'linked_user_access'
        verbose_name = "משתמש מקושר"
        verbose_name_plural = "משתמשים מקושרים"
        constraints = [
            # One row per pair, with the branch as a property of it. Widening
            # this to (owner, linked, branch) would let an unlimited row sit
            # beside a branch-limited one for the same colleague — Postgres
            # counts NULLs as distinct, so it would not even stop two unlimited
            # rows — and the screen could then show one name granting both a
            # single branch and everything at once.
            models.UniqueConstraint(fields=['owner', 'linked'], name='uniq_linked_user_pair'),
            # Linking an account to itself is always allowed implicitly; a row
            # saying so would only be a second, contradictable source of truth.
            models.CheckConstraint(
                check=~models.Q(owner=models.F('linked')),
                name='linked_user_not_self',
            ),
        ]

    def __str__(self):
        return f"{self.owner} → {self.linked}"


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


class RegistrationTerms(models.Model):
    """תקנון הרישום לווידג'ט — רשומה יחידה (pk=1)."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    content = models.TextField(verbose_name="תוכן HTML")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="עודכן בתאריך")

    class Meta:
        db_table = 'registration_terms'
        verbose_name = "תקנון רישום"
        verbose_name_plural = "תקנון רישום"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return "תקנון רישום"

