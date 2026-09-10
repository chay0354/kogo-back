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
