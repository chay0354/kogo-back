"""
תלמידים חיצוניים — the children in our external branches who never registered with us.

An external branch (``Branch.is_external``) runs our course with our instructor,
but the parents sign up through the municipality. Nobody pays us, no family
exists here, and once a month a sheet of names arrives on paper. Until now those
lessons held nothing at all: the register an instructor opened was empty, every
report said zero students, and salary tiers had no headcount to work from.

These two tables hold those children **as information only**. They are not
``Child`` rows and deliberately so. In this codebase almost nothing filters on
``Child.status``; what actually keeps a row out of the money is the enrollment
status plus the absence of a ``Payment``. A new kind of ``Child`` carrying an
active ``LessonEnrollment`` would have been read as a paying student by roughly
thirty-five queries, three of which cost real money — the second-child discount,
widget capacity, and ``find_existing_child_on_family``, which would have taken a
municipality row and quietly turned it into a paying registration.

So the isolation here is structural, not declarative. No ``Payment``,
``RecurringPayment``, ``Child`` or ``Family`` has a foreign key to these tables,
and nothing in ``apps.customers``, ``apps.documents`` or ``apps.store`` imports
this app. A query that looks for children cannot reach these rows — not because
someone remembered to exclude them, but because they are not there.

What they *do* reach: the instructor's register, the "did anyone mark this class"
badge, the low-enrollment alert, and a WhatsApp broadcast that needs nothing but
a phone and a name.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.enrollments.person_match import normalise_name


class ExternalStudent(models.Model):
    """One child on a municipality list, attached to one of our lessons."""

    SOURCE_MANUAL = 'manual'
    SOURCE_IMPORT = 'import'
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, 'ידני'),
        (SOURCE_IMPORT, 'מקובץ'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lesson = models.ForeignKey(
        'courses.Lesson',
        on_delete=models.CASCADE,
        related_name='external_students',
        verbose_name="שיעור",
    )
    first_name = models.CharField(max_length=60, verbose_name="שם פרטי")
    last_name = models.CharField(max_length=60, verbose_name="שם משפחה")
    phone = models.CharField(max_length=30, blank=True, verbose_name="טלפון")
    notes = models.TextField(blank=True, verbose_name="הערות")
    is_active = models.BooleanField(default=True, verbose_name="פעיל")
    start_date = models.DateField(null=True, blank=True, verbose_name="תאריך התחלה")
    end_date = models.DateField(null=True, blank=True, verbose_name="תאריך סיום")
    source = models.CharField(
        max_length=12,
        choices=SOURCE_CHOICES,
        default=SOURCE_MANUAL,
        verbose_name="מקור",
        help_text="ידני = הוקלד במשרד. מקובץ = נקרא מרשימת העירייה.",
    )
    # Kept on the row so the uniqueness constraint can be enforced by the
    # database rather than by whoever remembers to call the same helper.
    normalized_name = models.CharField(max_length=160, editable=False, default='')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='external_students_created',
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='external_students_updated',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'external_students'
        verbose_name = "תלמיד חיצוני"
        verbose_name_plural = "תלמידים חיצוניים"
        ordering = ['last_name', 'first_name']
        constraints = [
            # Only among the active rows: a child who left and came back should
            # not be refused because their old row is still there for history.
            models.UniqueConstraint(
                fields=['lesson', 'normalized_name'],
                condition=models.Q(is_active=True),
                name='uniq_active_external_student_per_lesson',
            ),
        ]
        indexes = [
            models.Index(fields=['lesson', 'is_active']),
        ]

    def __str__(self):
        return f'{self.full_name} ({self.lesson_id})'

    @property
    def full_name(self) -> str:
        return f'{self.first_name} {self.last_name}'.strip()

    def clean(self):
        """
        Refuse any lesson outside an external branch.

        A database CHECK cannot express this — ``is_external`` lives two tables
        away — so the rule is enforced here and in the serializer, and the real
        guarantee stays the structural one described in the module docstring.
        """
        super().clean()
        lesson = self.lesson if self.lesson_id else None
        branch = getattr(getattr(lesson, 'course', None), 'branch', None)
        if branch is not None and not branch.is_external:
            raise ValidationError(
                {'lesson': 'ניתן להוסיף תלמידים חיצוניים רק בסניף חיצוני'}
            )

    def save(self, *args, **kwargs):
        self.normalized_name = normalise_name(self.full_name)
        if self._state.adding:
            # The shell, the admin and a future importer all come through here;
            # only the API would otherwise be checked.
            self.full_clean(exclude=['normalized_name'])
        super().save(*args, **kwargs)


class ExternalStudentAttendance(models.Model):
    """
    A present/absent mark for one external student on one occurrence.

    Deliberately not ``LessonAttendance``: that table's ``child`` column is not
    nullable, its uniqueness is ``(lesson, child, occurrence_date)`` — which
    Postgres stops enforcing the moment ``child`` is NULL — and a dozen readers
    take ``child_id`` to be a real ``Child``. ``occurrence_date`` here is NOT
    NULL, unlike the legacy table, so there is no "no date" bucket to carry.
    """

    STATUS_CHOICES = [
        ('present', 'נוכח'),
        ('absent', 'נעדר'),
        ('not_marked', 'לא סומן'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    student = models.ForeignKey(
        ExternalStudent,
        on_delete=models.CASCADE,
        related_name='attendance_records',
        verbose_name="תלמיד חיצוני",
    )
    occurrence_date = models.DateField(verbose_name="תאריך מופע")
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='not_marked', verbose_name="סטטוס",
    )
    notes = models.TextField(blank=True, verbose_name="הערות")
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='external_student_marks',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'external_student_attendance'
        verbose_name = "נוכחות תלמיד חיצוני"
        verbose_name_plural = "נוכחות תלמידים חיצוניים"
        ordering = ['-occurrence_date']
        constraints = [
            models.UniqueConstraint(
                fields=['student', 'occurrence_date'],
                name='uniq_external_mark_per_occurrence',
            ),
        ]
        indexes = [
            models.Index(fields=['occurrence_date', 'status']),
        ]

    def __str__(self):
        return f'{self.student.full_name} - {self.occurrence_date} - {self.get_status_display()}'


class ExternalRosterImport(models.Model):
    """
    One municipality sheet, from upload until it is applied or thrown away.

    Every municipality sends a different shape, so nothing here assumes a
    layout. What it does assume is that a sheet is a list of *groups*, and the
    group is the unit of work throughout: reading, retrying, reviewing and
    applying all happen one group at a time. A five-page scan and a
    nine-block spreadsheet are the same object once they are segmented.

    Two things are deliberately absent. There is no field for an identity
    number, on this model or any of the three below — both real files carry
    one for every child and the owner's rule is that we never keep it. And for
    a spreadsheet there is no file: the grid is read in memory, the excluded
    columns are dropped, and only what is left is written, so there is no code
    path at all from that column to a stored byte.
    """

    KIND_XLSX = 'xlsx'
    KIND_PDF = 'pdf'
    KIND_CHOICES = [(KIND_XLSX, 'גיליון'), (KIND_PDF, 'סרוק')]

    STATUS_UPLOADED = 'uploaded'
    STATUS_PARSING = 'parsing'
    STATUS_PARSED = 'parsed'
    STATUS_APPLIED = 'applied'
    STATUS_FAILED = 'failed'
    STATUS_DISCARDED = 'discarded'
    STATUS_CHOICES = [
        (STATUS_UPLOADED, 'הועלה'),
        (STATUS_PARSING, 'בקריאה'),
        (STATUS_PARSED, 'נקרא'),
        (STATUS_APPLIED, 'הוחל'),
        (STATUS_FAILED, 'נכשל'),
        (STATUS_DISCARDED, 'בוטל'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(
        'core.Branch', on_delete=models.CASCADE,
        related_name='external_roster_imports', verbose_name="סניף",
    )
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, verbose_name="סוג קובץ")
    original_filename = models.CharField(max_length=255, verbose_name="שם הקובץ")
    byte_size = models.PositiveIntegerField(default=0, verbose_name="גודל")
    # Spreadsheets only: the grid with the excluded columns already removed.
    # It is the audit trail, what the review screen renders, and what a re-read
    # after a correction works from — so the file itself is never kept.
    sheet_grid = models.JSONField(default=list, blank=True, editable=False)
    period_label = models.CharField(max_length=120, blank=True, verbose_name="תקופה")
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_UPLOADED, verbose_name="סטטוס",
    )
    units_total = models.PositiveIntegerField(default=0, verbose_name="קבוצות בקובץ")
    units_done = models.PositiveIntegerField(default=0, verbose_name="קבוצות שנקראו")
    # 'סה"כ תושבים בדו"ח' — what the file claims about itself, checked against
    # what we actually read.
    stated_report_total = models.PositiveIntegerField(
        null=True, blank=True, verbose_name="סה\"כ לפי הקובץ",
    )
    error = models.TextField(blank=True, verbose_name="שגיאה")
    model_id = models.CharField(max_length=40, blank=True, editable=False)
    input_tokens = models.PositiveIntegerField(default=0, editable=False)
    output_tokens = models.PositiveIntegerField(default=0, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='external_roster_imports_created',
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='external_roster_imports_applied',
    )
    applied_at = models.DateTimeField(null=True, blank=True, verbose_name="תאריך החלה")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="תאריך יצירה")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="תאריך עדכון")

    class Meta:
        db_table = 'external_roster_imports'
        verbose_name = "ייבוא רשימת עירייה"
        verbose_name_plural = "ייבוא רשימות עירייה"
        ordering = ['-created_at']
        indexes = [models.Index(fields=['branch', 'status'])]

    def __str__(self):
        return f'{self.original_filename} ({self.branch_id})'

    @property
    def is_open(self) -> bool:
        """Still the manager's to finish — not applied, failed or thrown away."""
        return self.status in (self.STATUS_UPLOADED, self.STATUS_PARSING, self.STATUS_PARSED)


class ExternalRosterImportUnit(models.Model):
    """
    One municipality group inside a sheet: the thing that gets read, matched and applied.

    ``status`` is a small state machine, and the transition that matters is the
    claim. A unit is moved to ``running`` in its own committed transaction
    *before* the model is asked anything, so two callers can never take the same
    group, and a call that dies mid-flight is visible afterwards as ``running``
    with an old ``started_at`` rather than as work that quietly never happened.
    """

    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_PARSED = 'parsed'
    STATUS_MISMATCH = 'mismatch'
    STATUS_FAILED = 'failed'
    STATUS_SKIPPED = 'skipped'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'ממתין'),
        (STATUS_RUNNING, 'בקריאה'),
        (STATUS_PARSED, 'נקרא'),
        (STATUS_MISMATCH, 'לא תואם את הסכום בקובץ'),
        (STATUS_FAILED, 'נכשל'),
        (STATUS_SKIPPED, 'דולג'),
    ]

    MATCH_CONFIRMED = 'confirmed'
    MATCH_EXACT = 'exact'
    MATCH_AMBIGUOUS = 'ambiguous'
    MATCH_NONE = 'none'
    MATCH_CHOICES = [
        (MATCH_CONFIRMED, 'לפי שיוך קודם'),
        (MATCH_EXACT, 'התאמה מדויקת'),
        (MATCH_AMBIGUOUS, 'כמה אפשרויות'),
        (MATCH_NONE, 'לא נמצא שיעור'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    roster_import = models.ForeignKey(
        ExternalRosterImport, on_delete=models.CASCADE,
        related_name='units', verbose_name="ייבוא",
    )
    ordinal = models.PositiveIntegerField(verbose_name="סדר")
    municipality_code = models.CharField(max_length=40, blank=True, verbose_name="קוד קבוצה בעירייה")
    group_name = models.CharField(max_length=200, blank=True, verbose_name="שם הקבוצה")
    slots_raw = models.CharField(max_length=200, blank=True, verbose_name="ימים ושעות בקובץ")
    # [{'day': 1, 'start': '16:45'}] — parsed from slots_raw, matched on day and
    # start only. End times disagree between the municipality and us often
    # enough that comparing them loses real matches.
    slots = models.JSONField(default=list, blank=True)
    # Where this group's people live: row bounds for a sheet, a provider file id
    # for a scanned page. One of the two, never both.
    source_ref = models.JSONField(default=dict, blank=True, editable=False)
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING, verbose_name="סטטוס",
    )
    started_at = models.DateTimeField(null=True, blank=True, editable=False)
    duration_ms = models.PositiveIntegerField(default=0, editable=False)
    attempts = models.PositiveIntegerField(default=0, editable=False)
    error = models.TextField(blank=True, verbose_name="שגיאה")
    stated_total = models.PositiveIntegerField(
        null=True, blank=True, verbose_name="סה\"כ לפי הקובץ",
    )
    matched_lessons = models.ManyToManyField(
        'courses.Lesson', blank=True,
        related_name='external_roster_units', verbose_name="שיעורים מותאמים",
    )
    match_state = models.CharField(
        max_length=12, choices=MATCH_CHOICES, default=MATCH_NONE, verbose_name="מצב התאמה",
    )
    # Every lesson that could have been meant, so the screen can ask rather than
    # guess. An ambiguous match is a question for a person, not an error.
    candidates = models.JSONField(default=list, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'external_roster_import_units'
        verbose_name = "קבוצה בייבוא"
        verbose_name_plural = "קבוצות בייבוא"
        ordering = ['ordinal']
        constraints = [
            models.UniqueConstraint(
                fields=['roster_import', 'ordinal'], name='uniq_roster_unit_ordinal',
            ),
        ]
        indexes = [models.Index(fields=['status', 'started_at'])]

    def __str__(self):
        return f'{self.group_name or self.municipality_code} ({self.roster_import_id})'


class ExternalRosterImportRow(models.Model):
    """
    One child the sheet listed, and what applying the import would do about them.

    ``action`` is a proposal until a manager confirms the whole import. A row
    the file no longer lists becomes ``remove``; a row already on the lesson
    stays ``keep``; anything else is an ``add``.
    """

    ACTION_ADD = 'add'
    ACTION_KEEP = 'keep'
    ACTION_REMOVE = 'remove'
    ACTION_CHOICES = [
        (ACTION_ADD, 'הוספה'),
        (ACTION_KEEP, 'קיים'),
        (ACTION_REMOVE, 'הסרה'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    unit = models.ForeignKey(
        ExternalRosterImportUnit, on_delete=models.CASCADE,
        related_name='rows', verbose_name="קבוצה",
    )
    first_name = models.CharField(max_length=60, verbose_name="שם פרטי")
    last_name = models.CharField(max_length=60, blank=True, verbose_name="שם משפחה")
    phone = models.CharField(max_length=30, blank=True, verbose_name="טלפון")
    action = models.CharField(
        max_length=8, choices=ACTION_CHOICES, default=ACTION_ADD, verbose_name="פעולה",
    )
    existing_student = models.ForeignKey(
        ExternalStudent, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='roster_import_rows', verbose_name="תלמיד קיים",
    )
    edited = models.BooleanField(default=False, verbose_name="נערך ידנית")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'external_roster_import_rows'
        verbose_name = "שורה בייבוא"
        verbose_name_plural = "שורות בייבוא"
        ordering = ['last_name', 'first_name']
        indexes = [models.Index(fields=['unit', 'action'])]

    def __str__(self):
        return f'{self.first_name} {self.last_name}'.strip()

    @property
    def full_name(self) -> str:
        return f'{self.first_name} {self.last_name}'.strip()


class ExternalRosterGroup(models.Model):
    """
    A municipality's group code, tied to the lessons it means here.

    Written only when an import is applied, never while one is being looked at:
    a mapping a manager opened and abandoned must not shape next month. Once it
    exists, the same municipality's next sheet matches instantly and exactly,
    which is the difference between a monthly puzzle and a one-time setup.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(
        'core.Branch', on_delete=models.CASCADE,
        related_name='external_roster_groups', verbose_name="סניף",
    )
    municipality_code = models.CharField(max_length=40, verbose_name="קוד קבוצה בעירייה")
    group_name = models.CharField(max_length=200, blank=True, verbose_name="שם הקבוצה")
    lessons = models.ManyToManyField(
        'courses.Lesson', related_name='external_roster_groups', verbose_name="שיעורים",
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='external_roster_groups_confirmed',
    )
    confirmed_at = models.DateTimeField(null=True, blank=True, verbose_name="תאריך אישור")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'external_roster_groups'
        verbose_name = "שיוך קבוצת עירייה"
        verbose_name_plural = "שיוכי קבוצות עירייה"
        ordering = ['branch', 'municipality_code']
        constraints = [
            models.UniqueConstraint(
                fields=['branch', 'municipality_code'], name='uniq_roster_group_per_branch',
            ),
        ]

    def __str__(self):
        return f'{self.municipality_code} → {self.branch_id}'
