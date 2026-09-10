"""Reading and writing external students from the branch page."""
from __future__ import annotations

from rest_framework import serializers

from apps.courses.models import Lesson
from apps.enrollments.person_match import normalise_name
from apps.external_students.models import ExternalStudent

NOT_EXTERNAL_BRANCH = 'ניתן להוסיף תלמידים חיצוניים רק בסניף חיצוני'
DUPLICATE_ON_LESSON = 'תלמיד בשם הזה כבר רשום לשיעור הזה'


def _lesson_is_external(lesson: Lesson) -> bool:
    branch = getattr(getattr(lesson, 'course', None), 'branch', None)
    return bool(branch is not None and branch.is_external)


class ExternalStudentSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    lesson_id = serializers.UUIDField(source='lesson.id', read_only=True)
    course_id = serializers.UUIDField(source='lesson.course.id', read_only=True)
    course_name = serializers.CharField(source='lesson.course.name', read_only=True)
    course_display_id = serializers.IntegerField(source='lesson.course.display_id', read_only=True)
    branch_id = serializers.UUIDField(source='lesson.course.branch.id', read_only=True)
    day_of_week = serializers.IntegerField(source='lesson.day_of_week', read_only=True)
    start_time = serializers.TimeField(source='lesson.start_time', read_only=True, format='%H:%M')
    end_time = serializers.TimeField(source='lesson.end_time', read_only=True, format='%H:%M')
    attendance_count = serializers.SerializerMethodField()

    class Meta:
        model = ExternalStudent
        fields = [
            'id', 'lesson', 'lesson_id', 'course_id', 'course_name', 'course_display_id',
            'branch_id', 'day_of_week', 'start_time', 'end_time',
            'first_name', 'last_name', 'full_name', 'phone', 'notes',
            'is_active', 'start_date', 'end_date', 'source', 'attendance_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'source', 'created_at', 'updated_at']

    def get_attendance_count(self, obj) -> int:
        cached = getattr(obj, 'attendance_total', None)
        if cached is not None:
            return cached
        return obj.attendance_records.count()

    def validate_lesson(self, lesson):
        # A student never moves between lessons: moving one would carry their
        # attendance history to a class they were never in. Change = remove, re-add.
        if self.instance is not None and lesson.id != self.instance.lesson_id:
            raise serializers.ValidationError('לא ניתן להעביר תלמיד חיצוני בין שיעורים')
        if not _lesson_is_external(lesson):
            raise serializers.ValidationError(NOT_EXTERNAL_BRANCH)
        return lesson

    def validate(self, data):
        lesson = data.get('lesson') or (self.instance.lesson if self.instance else None)
        first = data.get('first_name', getattr(self.instance, 'first_name', ''))
        last = data.get('last_name', getattr(self.instance, 'last_name', ''))
        is_active = data.get('is_active', getattr(self.instance, 'is_active', True))

        if lesson is not None and is_active:
            name_key = normalise_name(f'{first} {last}')
            clash = ExternalStudent.objects.filter(
                lesson=lesson, is_active=True, normalized_name=name_key,
            )
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError({'first_name': DUPLICATE_ON_LESSON})
        return data


class ExternalStudentBulkSerializer(serializers.Serializer):
    """One lesson, many names — the shape a typed-up paper list actually has."""

    lesson = serializers.PrimaryKeyRelatedField(queryset=Lesson.objects.all())
    students = serializers.ListField(child=serializers.DictField(), allow_empty=False, max_length=200)

    def validate_lesson(self, lesson):
        if not _lesson_is_external(lesson):
            raise serializers.ValidationError(NOT_EXTERNAL_BRANCH)
        return lesson


class ExternalBroadcastSerializer(serializers.Serializer):
    """
    Send one ManyChat flow to the phones on a list of external students.

    ``automation_type`` is fixed to 'flow' by the view: the Kogo registration
    templates are built around a paying registration's course, day and time, and
    would reach a municipality parent full of dashes.
    """

    student_ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=False)
    automation_id = serializers.CharField()
    dry_run = serializers.BooleanField(default=True)
    skip_phones = serializers.ListField(child=serializers.CharField(), required=False, default=list)
