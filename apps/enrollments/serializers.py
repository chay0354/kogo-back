from rest_framework import serializers
from apps.enrollments.models import Enrollment, LessonEnrollment, ChildAbsence


class EnrollmentSerializer(serializers.ModelSerializer):
    """Serializer for Course Enrollment"""
    course_name = serializers.CharField(source='course.name', read_only=True)
    child_name = serializers.CharField(source='child.full_name', read_only=True)
    
    class Meta:
        model = Enrollment
        fields = ['id', 'course', 'course_name', 'child', 'child_name', 'is_active', 'enrolled_at', 'created_at']
        read_only_fields = ['id', 'enrolled_at', 'created_at']


class LessonEnrollmentSerializer(serializers.ModelSerializer):
    """Serializer for Lesson Enrollment"""
    lesson_info = serializers.SerializerMethodField()
    child_name = serializers.CharField(source='child.full_name', read_only=True)
    
    class Meta:
        model = LessonEnrollment
        fields = ['id', 'lesson', 'lesson_info', 'child', 'child_name', 'status', 'start_date', 'end_date', 'notes', 'created_at']
        read_only_fields = ['id', 'created_at']
    
    def get_lesson_info(self, obj):
        """
        USAGE: Used in LessonEnrollmentSerializer to provide lesson details
        """
        if obj.lesson:
            return {
                'course_name': obj.lesson.course.name,
                'day_of_week': obj.lesson.day_of_week,
                'start_time': obj.lesson.start_time.strftime('%H:%M'),
                'end_time': obj.lesson.end_time.strftime('%H:%M'),
            }
        return None
    
    def validate(self, data):
        """Validate that lesson has capacity for new enrollment"""
        lesson = data.get('lesson')
        child = data.get('child')
        
        if not lesson:
            return data
        
        # Skip capacity check if updating existing enrollment
        if self.instance:
            return data
        
        # Determine effective capacity: explicit lesson cap takes precedence,
        # otherwise fall back to room capacity. A room is required only when
        # there is no per-lesson cap to fall back on.
        lesson_cap = lesson.max_students
        room_capacity = lesson.room.capacity if lesson.room else None

        if lesson_cap is None and room_capacity is None:
            raise serializers.ValidationError({
                'lesson': 'לא ניתן להירשם לשיעור ללא חדר מוגדר'
            })

        if lesson_cap is not None and room_capacity is not None:
            effective_capacity = min(lesson_cap, room_capacity)
        else:
            effective_capacity = lesson_cap if lesson_cap is not None else room_capacity

        # Count current active enrollments
        active_enrollments = LessonEnrollment.objects.filter(
            lesson=lesson,
            status='active'
        ).count()

        if active_enrollments >= effective_capacity:
            raise serializers.ValidationError({
                'lesson': f'השיעור מלא - קיבולת מקסימלית: {effective_capacity} תלמידים'
            })

        return data


class AbsenceHistorySerializer(serializers.ModelSerializer):
    """Serializer for Child Absence History"""
    lesson_name = serializers.SerializerMethodField()
    course_name = serializers.CharField(source='course.name', read_only=True)
    
    class Meta:
        model = ChildAbsence
        fields = ['id', 'lesson_name', 'course_name', 'occurrence_date', 'created_at']
        read_only_fields = ['id', 'created_at']
    
    def get_lesson_name(self, obj):
        """
        USAGE: Returns lesson display name with day and time
        """
        if obj.lesson:
            day_names = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']
            day_name = day_names[obj.lesson.day_of_week] if obj.lesson.day_of_week is not None else ''
            time_str = f"{obj.lesson.start_time.strftime('%H:%M')}" if obj.lesson.start_time else ''
            return f"{day_name} {time_str}".strip()
        return '-'

