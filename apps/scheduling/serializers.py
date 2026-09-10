from rest_framework import serializers
from apps.courses.models import Lesson
from apps.enrollments.enrollment_counts import count_paying_enrollments, is_paying_enrollment
from apps.enrollments.models import LessonAttendance


class LessonListSerializer(serializers.ModelSerializer):
    """Serializer for calendar view"""
    course_name = serializers.CharField(source='course.name', read_only=True)
    course_display_id = serializers.IntegerField(source='course.display_id', read_only=True)
    course_type_name = serializers.CharField(source='course.course_type.name', read_only=True)
    instructor_id = serializers.UUIDField(source='instructor.id', read_only=True)
    instructor_name = serializers.CharField(source='instructor.full_name', read_only=True)
    branch_id = serializers.UUIDField(source='course.branch.id', read_only=True)
    branch_name = serializers.CharField(source='course.branch.name', read_only=True)
    city_id = serializers.UUIDField(source='course.branch.city.id', read_only=True, allow_null=True)
    city_name = serializers.CharField(source='course.branch.city.name', read_only=True, allow_null=True)
    room_name = serializers.CharField(source='room.name', read_only=True, allow_null=True)
    room_capacity = serializers.IntegerField(source='course.capacity', read_only=True)
    enrollment_count = serializers.SerializerMethodField()
    day_of_week_display = serializers.CharField(source='get_day_of_week_display', read_only=True)
    
    class Meta:
        model = Lesson
        fields = [
            'id', 'course_name', 'course_display_id', 'course_type_name',
            'instructor_id', 'instructor_name',
            'branch_id', 'branch_name', 'city_id', 'city_name',
            'room_name', 'room_capacity', 'day_of_week', 'day_of_week_display',
            'start_time', 'end_time', 'lesson_date',
            'status', 'cancellation_reason', 'cancelled_at', 'enrollment_count', 'notes',
            'is_recurring'
        ]
    
    def get_enrollment_count(self, obj):
        annotated = getattr(obj, 'paying_enrollment_count', None)
        if annotated is not None:
            return annotated
        if hasattr(obj, '_prefetched_objects_cache') and 'enrollments' in obj._prefetched_objects_cache:
            return sum(1 for e in obj.enrollments.all() if is_paying_enrollment(e))
        return count_paying_enrollments(lesson=obj)


class LessonDetailSerializer(serializers.ModelSerializer):
    """Detailed lesson with enrollments and attendance"""
    course_name = serializers.CharField(source='course.name', read_only=True)
    course_display_id = serializers.IntegerField(source='course.display_id', read_only=True)
    course_type_name = serializers.CharField(source='course.course_type.name', read_only=True)
    instructor_id = serializers.UUIDField(source='instructor.id', read_only=True)
    instructor_name = serializers.CharField(source='instructor.full_name', read_only=True)
    instructor_email = serializers.CharField(source='instructor.email', read_only=True)
    branch_id = serializers.UUIDField(source='course.branch.id', read_only=True)
    branch_name = serializers.CharField(source='course.branch.name', read_only=True)
    city_id = serializers.UUIDField(source='course.branch.city.id', read_only=True, allow_null=True)
    city_name = serializers.CharField(source='course.branch.city.name', read_only=True, allow_null=True)
    room_name = serializers.CharField(source='room.name', read_only=True, allow_null=True)
    room_capacity = serializers.IntegerField(source='course.capacity', read_only=True)
    enrollments = serializers.SerializerMethodField()
    attendance = serializers.SerializerMethodField()
    cancellation_reason = serializers.SerializerMethodField()
    
    class Meta:
        model = Lesson
        fields = [
            'id', 'course_name', 'course_display_id', 'course_type_name',
            'instructor_id', 'instructor_name', 'instructor_email',
            'branch_id', 'branch_name', 'city_id', 'city_name', 'room_name', 'room_capacity',
            'day_of_week', 'start_time', 'end_time',
            'lesson_date', 'status', 'cancellation_reason', 'cancelled_at', 'notes', 'is_recurring',
            'enrollments', 'attendance', 'created_at', 'updated_at'
        ]
    
    def get_enrollments(self, obj):
        enrollments = obj.enrollments.all()
        if hasattr(obj, '_prefetched_objects_cache') and 'enrollments' in obj._prefetched_objects_cache:
            enrollments = [e for e in enrollments if e.status == 'active']
        else:
            enrollments = enrollments.filter(status='active').select_related('child')
        return [{
            'id': str(e.id),
            'child_id': str(e.child.id),
            'child_name': e.child.full_name,
            'child_status': e.child.status,
        } for e in enrollments]
    
    def get_attendance(self, obj):
        if hasattr(obj, '_prefetched_objects_cache') and 'attendance_records' in obj._prefetched_objects_cache:
            attendance = list(obj.attendance_records.all())
        else:
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
    """
    One mark, for a real child or for an external student.

    ``child_id`` alone is still accepted: the two repositories deploy
    separately, and a frontend that has not shipped yet must keep working.
    ``attendee_kind`` says which table ``attendee_id`` belongs to, so the view
    never has to guess — a wrong guess would be a 500, not a 404.
    """

    child_id = serializers.UUIDField(required=False)
    attendee_id = serializers.UUIDField(required=False)
    attendee_kind = serializers.ChoiceField(
        choices=['child', 'external'], required=False, default='child',
    )
    status = serializers.ChoiceField(choices=['present', 'absent', 'not_marked'])

    def validate(self, data):
        if not data.get('attendee_id') and not data.get('child_id'):
            raise serializers.ValidationError('חסר מזהה תלמיד')
        data['attendee_id'] = data.get('attendee_id') or data['child_id']
        return data


class AttendanceSerializer(serializers.ModelSerializer):
    """Serializer for attendance records"""
    child_id = serializers.UUIDField(source='child.id', read_only=True)
    child_name = serializers.CharField(source='child.full_name', read_only=True)
    child_status = serializers.CharField(source='child.status', read_only=True)
    lesson_id = serializers.UUIDField(source='lesson.id', read_only=True)
    occurrence_date = serializers.DateField(read_only=True)
    # Same identity pair the roster rows carry, so the client keys both lists
    # the same way. For a real child it is simply the child id again.
    attendee_id = serializers.UUIDField(source='child.id', read_only=True)
    attendee_kind = serializers.SerializerMethodField()

    class Meta:
        model = LessonAttendance
        fields = ['id', 'lesson_id', 'occurrence_date', 'child_id', 'child_name', 'child_status', 'status', 'notes', 'created_at', 'attendee_id', 'attendee_kind']
        read_only_fields = ['id', 'created_at']

    def get_attendee_kind(self, obj):
        return 'child'

