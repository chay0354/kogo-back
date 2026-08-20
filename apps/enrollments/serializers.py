from rest_framework import serializers

from apps.enrollments.enrollment_counts import count_capacity_enrollments
from apps.enrollments.trial_reminders import compute_trial_lesson_date
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
    trial_registration = serializers.BooleanField(
        default=False,
        write_only=True,
        help_text='When true, marks child as trial_signed and sends test-lesson-register WhatsApp immediately.',
    )

    class Meta:
        model = LessonEnrollment
        fields = [
            'id', 'lesson', 'lesson_info', 'child', 'child_name', 'status',
            'bundle', 'start_date', 'end_date', 'notes', 'trial_registration', 'created_at',
        ]
        read_only_fields = ['id', 'created_at']
    
    def get_lesson_info(self, obj):
        """
        USAGE: Used in LessonEnrollmentSerializer to provide lesson details
        """
        if obj.lesson:
            return {
                'course_name': obj.lesson.course.name,
                'course_display_id': obj.lesson.course.display_id,
                'day_of_week': obj.lesson.day_of_week,
                'start_time': obj.lesson.start_time.strftime('%H:%M'),
                'end_time': obj.lesson.end_time.strftime('%H:%M'),
            }
        return None
    
    def validate(self, data):
        """Validate that lesson has capacity for new enrollment"""
        lesson = data.get('lesson')
        child = data.get('child')
        bundle = data.get('bundle')

        if not lesson:
            return data

        if bundle and not bundle.lessons.filter(pk=lesson.pk).exists():
            raise serializers.ValidationError({
                'bundle': 'השיעור אינו חלק מהמסלול המשולב שנבחר'
            })

        # Skip capacity check if updating existing enrollment
        if self.instance:
            return data

        if not lesson.room:
            raise serializers.ValidationError({
                'lesson': 'לא ניתן להירשם לשיעור ללא חדר מוגדר'
            })

        trial_registration = data.get('trial_registration', False)
        occurrence_date = None
        if trial_registration:
            occurrence_date = data.get('trial_lesson_date') or compute_trial_lesson_date(lesson)

        capacity = lesson.course.capacity or lesson.room.capacity
        current = count_capacity_enrollments(
            lesson=lesson,
            occurrence_date=occurrence_date if trial_registration else None,
        )
        if current >= capacity:
            raise serializers.ValidationError({
                'lesson': f'השיעור מלא - קיבולת מקסימלית: {capacity} תלמידים'
            })
        
        return data

    def create(self, validated_data):
        validated_data.pop('trial_registration', None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop('trial_registration', None)
        return super().update(instance, validated_data)


class AbsenceHistorySerializer(serializers.ModelSerializer):
    """Serializer for Child Absence History"""
    lesson_name = serializers.SerializerMethodField()
    course_name = serializers.CharField(source='course.name', read_only=True)
    course_display_id = serializers.IntegerField(source='course.display_id', read_only=True)

    class Meta:
        model = ChildAbsence
        fields = ['id', 'lesson_name', 'course_name', 'course_display_id', 'occurrence_date', 'created_at']
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

