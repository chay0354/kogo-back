import uuid
from decimal import Decimal
from django.conf import settings
from django.db import connection, models
from apps.core.models import Branch, Room
from apps.instructors.models import Instructor


def _next_course_display_id():
    """Atomically pull the next value from the DB-side course_display_id_seq sequence.

    Course.id is a UUID primary key, so this real Postgres sequence backs a short,
    human-friendly, race-safe display number shown next to the group's name in the UI.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval('course_display_id_seq')")
        return cursor.fetchone()[0]


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
    display_id = models.PositiveIntegerField(
        unique=True,
        editable=False,
        default=_next_course_display_id,
        verbose_name="מספר קבוצה",
        help_text="מספר סידורי קצר לזיהוי הקבוצה (לא מפתח ראשי), מוצג לצד שם הקבוצה בממשק.",
    )
    course_type = models.ForeignKey(CourseType, on_delete=models.CASCADE, related_name='courses', null=True, blank=True, verbose_name="תחום")
    name = models.CharField(max_length=200, verbose_name="שם החוג")
    description = models.TextField(blank=True, verbose_name="תיאור")
    price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="מחיר")
    capacity = models.PositiveIntegerField(verbose_name="קיבולת")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='courses', verbose_name="סניף")
    min_age = models.PositiveIntegerField(null=True, blank=True, verbose_name="גיל מינימום")
    max_age = models.PositiveIntegerField(null=True, blank=True, verbose_name="גיל מקסימום")
    is_active = models.BooleanField(default=True, verbose_name="פעיל")
    is_adult = models.BooleanField(default=False, verbose_name="18+")
    must_attend_all_lessons = models.BooleanField(default=False, verbose_name="מחוייב בכל השיעורים")
    trial_lesson_is_paid = models.BooleanField(
        default=False,
        verbose_name="שיעור ניסיון בתשלום",
        help_text="כאשר מופעל, הרשמה לשיעור ניסיון דרך הווידג'ט תחייב את המחיר שמוגדר.",
    )
    trial_lesson_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="מחיר שיעור ניסיון (₪)",
    )
    registration_fee_override = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="דמי רישום מותאמים (₪)",
        help_text="אם מוגדר, מחליף את דמי הרישום הכלליים רק לחוג זה.",
    )
    charge_standing_order_immediately = models.BooleanField(
        default=False,
        verbose_name="חיוב הוראת קבע מיידי",
        help_text="כאשר מופעל, הוראת הקבע של החוג הזה מחויבת מהיום ולא מתאריך תחילת העונה הכללי.",
    )
    external_link = models.CharField(
        max_length=500,
        blank=True,
        verbose_name="לינק חיצוני לחוג",
        help_text="לינק הרשמה חיצוני ספציפי לחוג זה (רלוונטי רק לסניפים חיצוניים). ריק = שימוש בלינק של הסניף.",
    )
    instructor = models.ForeignKey(
        Instructor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='courses',
        verbose_name="מדריך",
    )
    instructor_salary_override = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="שכר מדריך חודשי לקבוצה",
        help_text="תשלום חודשי למדריך עבור הקבוצה (לא לשיעור בודד).",
    )
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

    def sync_instructor_to_lessons(self, previous_instructor=None):
        """Propagate team instructor to lessons that still follow the previous one.

        Lessons with a different instructor (for example a combined-track
        override) are left alone. Monthly pay stays on the course.
        """
        qs = self.lessons.all()
        if previous_instructor is not None:
            qs = qs.filter(instructor=previous_instructor)
        qs.update(
            instructor=self.instructor,
            instructor_salary_override=None,
        )


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


class LessonBundle(models.Model):
    """
    מסלול משולב - a combined-price package of 2+ Lessons belonging to the
    same Course (e.g. "twice a week" — Sunday + Wednesday sold together at
    a discounted combined price). Registering for the bundle creates a
    separate LessonEnrollment per member lesson (see LessonEnrollment.bundle),
    each billed at the widget combined_price on the first member lesson only
    (one standing order for the full monthly amount). Extra days enroll at ₪0.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='lesson_bundles', verbose_name="חוג")
    lessons = models.ManyToManyField(Lesson, related_name='bundles', verbose_name="שיעורים במסלול")
    name = models.CharField(max_length=200, blank=True, verbose_name="שם המסלול")
    combined_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="מחיר משולב")
    min_age = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="גיל מינימום בווידג'ט",
        help_text="ריק = אותה קבוצת גיל כמו החוג. כשמוגדר, המסלול יופיע גם כשבוחרים גיל זה בווידג'ט.",
    )
    max_age = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="גיל מקסימום בווידג'ט",
        help_text="ריק = אותה קבוצת גיל כמו החוג.",
    )
    is_active = models.BooleanField(default=True, verbose_name="פעיל")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'lesson_bundles'
        verbose_name = "מסלול משולב"
        verbose_name_plural = "מסלולים משולבים"
        ordering = ['course', 'name']

    def __str__(self):
        return self.name or f"מסלול משולב - {self.course.name}"

    def price_per_lesson(self):
        count = self.lessons.count()
        if not count:
            return self.combined_price
        return (self.combined_price / count).quantize(Decimal('0.01'))


class LessonPriceOption(models.Model):
    """
    מחיר נוסף לשיעור — an extra catalog entry for the same Lesson slot.

    The widget lists the default lesson row (course name + course price) plus
    one row per active price option (display_title + monthly_price). Two parents
    can register for the same lesson at different prices.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lesson = models.ForeignKey(
        Lesson,
        on_delete=models.CASCADE,
        related_name='price_options',
        verbose_name="שיעור",
    )
    display_title = models.CharField(max_length=200, verbose_name="כותרת בווידג'ט")
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="מחיר חודשי")
    min_age = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="גיל מינימום בווידג'ט",
        help_text="ריק = אותה קבוצת גיל כמו החוג. כשמוגדר, השורה תופיע גם כשבוחרים גיל זה בווידג'ט.",
    )
    max_age = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="גיל מקסימום בווידג'ט",
        help_text="ריק = אותה קבוצת גיל כמו החוג.",
    )
    sort_order = models.PositiveIntegerField(default=0, verbose_name="סדר תצוגה")
    is_active = models.BooleanField(default=True, verbose_name="פעיל")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'lesson_price_options'
        verbose_name = "מחיר נוסף לשיעור"
        verbose_name_plural = "מחירים נוספים לשיעור"
        ordering = ['sort_order', 'display_title']

    def __str__(self):
        return f"{self.display_title} — ₪{self.monthly_price}"


