from rest_framework import serializers
from apps.courses.models import Lesson
from apps.enrollments.enrollment_counts import count_paying_enrollments, is_paying_enrollment
from apps.enrollments.models import LessonAttendance


class LessonListSerializer(serializers.ModelSerializer):
    """Serializer for calendar view"""
    course_name = serializers.CharField(source='course.name', read_only=True)
    course_type_name = serializers.CharField(source='course.course_type.name', read_only=True)
    instructor_id = serializers.UUIDField(source='instructor.id', read_only=True)
    instructor_name = serializers.CharField(source='instructor.full_name', read_only=True)
    branch_id = serializers.UUIDField(source='branch.id', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    room_name = serializers.CharField(source='room.name', read_only=True, allow_null=True)
    enrollment_count = serializers.SerializerMethodField()
    day_of_week_display = serializers.CharField(source='get_day_of_week_display', read_only=True)
    
    class Meta:
        model = Lesson
        fields = [
            'id', 'course_name', 'course_type_name',
            'instructor_id', 'instructor_name',
            'branch_id', 'branch_name',
            'room_name', 'day_of_week', 'day_of_week_display',
            'start_time', 'end_time', 'lesson_date',
            'status', 'cancellation_reason', 'cancelled_at', 'enrollment_count', 'notes',
            'is_recurring'
        ]
    
    def get_enrollment_count(self, obj):
        if hasattr(obj, '_prefetched_objects_cache') and 'enrollments' in obj._prefetched_objects_cache:
            return sum(1 for e in obj.enrollments.all() if is_paying_enrollment(e))
        return count_paying_enrollments(lesson=obj)


class LessonDetailSerializer(serializers.ModelSerializer):
    """Detailed lesson with enrollments and attendance"""
    course_name = serializers.CharField(source='course.name', read_only=True)
    course_type_name = serializers.CharField(source='course.course_type.name', read_only=True)
    instructor_id = serializers.UUIDField(source='instructor.id', read_only=True)
    instructor_name = serializers.CharField(source='instructor.full_name', read_only=True)
    instructor_email = serializers.EmailField(source='instructor.email', read_only=True)
    branch_id = serializers.UUIDField(source='branch.id', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    room_name = serializers.CharField(source='room.name', read_only=True, allow_null=True)
    enrollments = serializers.SerializerMethodField()
    attendance = serializers.SerializerMethodField()
    cancellation_reason = serializers.SerializerMethodField()
    
    class Meta:
        model = Lesson
        fields = [
            'id', 'course_name', 'course_type_name',
            'instructor_id', 'instructor_name', 'instructor_email',
            'branch_id', 'branch_name', 'room_name',
            'day_of_week', 'start_time', 'end_time',
            'lesson_date', 'status', 'cancellation_reason', 'cancelled_at', 'notes', 'is_recurring',
            'enrollments', 'attendance', 'created_at', 'updated_at'
        ]
    
    def get_enrollments(self, obj):
        enrollments = obj.enrollments.filter(status='active').select_related('child')
        return [{
            'id': str(e.id),
            'child_id': str(e.child.id),
            'child_name': e.child.full_name,
            'child_status': e.child.status,
        } for e in enrollments]
    
    def get_attendance(self, obj):
        attendance = obj.attendance_records.select_related('child')
        return [{
            'id': str(a.id),
            'child_id': str(a.child.id),
            'child_name': a.child.full_name,
            'status': a.status,
            'child_status': a.child.status,
        } for a in attendance]

    def get_cancellation_reason(self, obj):
        # Prefer structured field; fallback to legacy "בוטל: <reason>" prefix in notes.
        reason = getattr(obj, 'cancellation_reason', None)
        if reason:
            return reason
        notes = (getattr(obj, 'notes', '') or '').strip()
        if notes.startswith('בוטל:'):
            first_line = notes.splitlines()[0]
            # "בוטל: reason"
            return first_line.replace('בוטל:', '', 1).strip() or None
        return None


class LessonCancelSerializer(serializers.Serializer):
    """Serializer for cancelling a lesson"""
    reason = serializers.CharField(required=False, allow_blank=True)
    date = serializers.DateField(required=False)


class AttendanceMarkSerializer(serializers.Serializer):
    """Serializer for marking attendance"""
    child_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=['present', 'absent', 'not_marked'])


class AttendanceSerializer(serializers.ModelSerializer):
    """Serializer for attendance records"""
    child_id = serializers.UUIDField(source='child.id', read_only=True)
    child_name = serializers.CharField(source='child.full_name', read_only=True)
    child_status = serializers.CharField(source='child.status', read_only=True)
    lesson_id = serializers.UUIDField(source='lesson.id', read_only=True)
    occurrence_date = serializers.DateField(read_only=True)
    
    class Meta:
        model = LessonAttendance
        fields = ['id', 'lesson_id', 'occurrence_date', 'child_id', 'child_name', 'child_status', 'status', 'notes', 'created_at']
        read_only_fields = ['id', 'created_at']

