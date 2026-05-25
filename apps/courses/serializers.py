from decimal import Decimal, InvalidOperation

from rest_framework import serializers
from django.db.models import Count, Q
from apps.courses.models import CourseType, Course, Lesson
from apps.enrollments.models import LessonEnrollment
from apps.core.models import Branch
from apps.scheduling.studio_conflict import timed_event_conflicts_lesson


def _normalize_additional_course_prices(value):
    """
    Coerce a list of `{course_index, price}` tier entries into a clean,
    sorted, deduped list. Used by both LessonSerializer and
    LessonWithEnrollmentsSerializer.

    Rules:
    - Empty / null / not-a-list inputs become [].
    - Each entry must be a dict-like object.
    - course_index must parse as int and be >= 2.
    - price must parse as a non-negative decimal; entries with blank/None price
      are dropped (treated as "tier removed").
    - Later entries with the same course_index override earlier ones.
    - Output is sorted ascending by course_index.
    """
    if value in (None, ''):
        return []
    if not isinstance(value, list):
        raise serializers.ValidationError(
            'מחירים מדורגים חייבים להיות רשימה של {course_index, price}'
        )

    cleaned: dict[int, float] = {}
    for entry in value:
        if not isinstance(entry, dict):
            raise serializers.ValidationError(
                'כל מדרגת מחיר חייבת להיות אובייקט עם course_index ו-price'
            )
        try:
            idx = int(entry.get('course_index'))
        except (TypeError, ValueError):
            raise serializers.ValidationError('course_index חייב להיות מספר שלם >= 2')
        if idx < 2:
            raise serializers.ValidationError('course_index חייב להיות 2 או יותר')

        price_raw = entry.get('price')
        if price_raw in (None, ''):
            continue
        try:
            price = Decimal(str(price_raw))
        except (InvalidOperation, TypeError, ValueError):
            raise serializers.ValidationError('price חייב להיות מספר תקין')
        if price < 0:
            raise serializers.ValidationError('price לא יכול להיות שלילי')

        cleaned[idx] = float(price)

    return [
        {'course_index': idx, 'price': cleaned[idx]}
        for idx in sorted(cleaned)
    ]


class CourseTypeSerializer(serializers.ModelSerializer):
    """Basic CourseType serializer for CRUD operations"""
    
    class Meta:
        model = CourseType
        fields = ['id', 'name', 'description', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class BranchMinimalSerializer(serializers.ModelSerializer):
    """Minimal branch info for nested serialization"""
    
    class Meta:
        model = Branch
        fields = ['id', 'name']


class CourseTypeWithStatsSerializer(serializers.ModelSerializer):
    """CourseType with aggregated statistics for catalog view"""
    courses_count = serializers.SerializerMethodField()
    lessons_count = serializers.SerializerMethodField()
    students_count = serializers.SerializerMethodField()
    branches = serializers.SerializerMethodField()
    
    class Meta:
        model = CourseType
        fields = ['id', 'name', 'description', 'courses_count', 'lessons_count', 
                  'students_count', 'branches', 'is_active']
    
    def get_courses_count(self, obj):
        """Count active courses under this course type"""
        return obj.courses.filter(is_active=True).count()
    
    def get_lessons_count(self, obj):
        """Count recurring lessons under this course type"""
        return Lesson.objects.filter(
            course__course_type=obj,
            is_recurring=True
        ).count()
    
    def get_students_count(self, obj):
        """Count active enrolled students under this course type"""
        return LessonEnrollment.objects.filter(
            lesson__course__course_type=obj,
            status='active'
        ).count()
    
    def get_branches(self, obj):
        """Get distinct branches that host courses of this course type"""
        branches = Branch.objects.filter(
            courses__course_type=obj
        ).distinct()
        return BranchMinimalSerializer(branches, many=True).data


class InstructorMinimalSerializer(serializers.Serializer):
    """Minimal instructor info for nested serialization"""
    id = serializers.UUIDField()
    full_name = serializers.CharField()
    salary_model_type = serializers.CharField()
    fixed_salary_per_lesson = serializers.SerializerMethodField()
    salary_tiers = serializers.SerializerMethodField()
    
    def get_fixed_salary_per_lesson(self, obj):
        """Get fixed salary, ensuring it's always a number"""
        salary = getattr(obj, 'fixed_salary_per_lesson', None)
        if salary is None:
            return 0.0  # Default salary
        return float(salary)
    
    def get_salary_tiers(self, obj):
        """Get instructor salary tiers"""
        if hasattr(obj, 'salary_tiers'):
            return [{
                'min_students': tier.min_students,
                'max_students': tier.max_students,
                'salary_per_lesson': float(tier.salary_per_lesson)
            } for tier in obj.salary_tiers.all()]
        return []


class RoomMinimalSerializer(serializers.Serializer):
    """Minimal room info for nested serialization"""
    id = serializers.UUIDField()
    name = serializers.CharField()


class LessonWithEnrollmentsSerializer(serializers.ModelSerializer):
    """Lesson with enrollment count and related info"""
    room = RoomMinimalSerializer(read_only=True)
    instructor = InstructorMinimalSerializer(read_only=True)
    enrolled_count = serializers.SerializerMethodField()
    total_students_count = serializers.SerializerMethodField()
    day_name = serializers.SerializerMethodField()

    class Meta:
        model = Lesson
        fields = ['id', 'day_of_week', 'day_name', 'start_time', 'end_time',
                  'room', 'instructor', 'enrolled_count', 'total_students_count',
                  'price', 'lesson_price_override', 'additional_course_prices',
                  'instructor_salary_override', 'status', 'is_recurring', 'notes']

    def validate_additional_course_prices(self, value):
        return _normalize_additional_course_prices(value)

    def get_enrolled_count(self, obj):
        """Get count of active enrollments for this lesson (for income calculation)"""
        return obj.enrollments.filter(status='active').count()
    
    def get_total_students_count(self, obj):
        """Get count of all enrollments regardless of status (for student count display)"""
        return obj.enrollments.count()
    
    def get_day_name(self, obj):
        """Convert day number to Hebrew name"""
        days = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']
        return days[obj.day_of_week] if 0 <= obj.day_of_week < 7 else ''


class CourseWithLessonsSerializer(serializers.ModelSerializer):
    """Course with nested lessons for details view"""
    lessons = LessonWithEnrollmentsSerializer(many=True, read_only=True)
    
    class Meta:
        model = Course
        fields = ['id', 'name', 'description', 'price', 'capacity', 
                  'min_age', 'max_age', 'lessons', 'is_active']


class CourseTypeDetailsSerializer(serializers.ModelSerializer):
    """Complete course type details with courses and lessons"""
    courses = CourseWithLessonsSerializer(many=True, read_only=True)
    
    class Meta:
        model = CourseType
        fields = ['id', 'name', 'description', 'courses', 'is_active']


class CourseSerializer(serializers.ModelSerializer):
    """Basic Course serializer for CRUD operations"""
    course_type_name = serializers.CharField(source='course_type.name', read_only=True, allow_null=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    lessons_count = serializers.SerializerMethodField()
    enrolled_students_count = serializers.SerializerMethodField()
    lessons = serializers.SerializerMethodField()
    
    class Meta:
        model = Course
        fields = ['id', 'course_type', 'course_type_name', 'name', 'description', 
                  'price', 'capacity', 'branch', 'branch_name',
                  'min_age', 'max_age', 'is_active', 'lessons_count', 'enrolled_students_count',
                  'lessons', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_lessons_count(self, obj):
        """Count lessons for this course"""
        return obj.lessons.filter(status='scheduled').count()
    
    def get_enrolled_students_count(self, obj):
        """Count enrolled students across all lessons of this course"""
        return LessonEnrollment.objects.filter(
            lesson__course=obj,
            status='active'
        ).values('child').distinct().count()
    
    def get_lessons(self, obj):
        """Get lessons for this course (only for detail view)"""
        # Only include lessons if specifically requested (to avoid overhead in list views)
        request = self.context.get('request')
        if request and request.query_params.get('include_lessons') == 'true':
            lessons = obj.lessons.filter(status='scheduled').select_related('room', 'instructor')
            return [{
                'id': str(lesson.id),
                'day_of_week': lesson.day_of_week,
                'start_time': lesson.start_time.strftime('%H:%M'),
                'end_time': lesson.end_time.strftime('%H:%M'),
                'room_name': lesson.room.name if lesson.room else None,
                'instructor_name': lesson.instructor.full_name if lesson.instructor else None,
            } for lesson in lessons]
        return []


class LessonSerializer(serializers.ModelSerializer):
    """Basic Lesson serializer for CRUD operations"""
    course_name = serializers.CharField(source='course.name', read_only=True)
    room_name = serializers.CharField(source='room.name', read_only=True, allow_null=True)
    instructor_name = serializers.CharField(source='instructor.full_name', read_only=True, allow_null=True)
    day_name = serializers.SerializerMethodField()
    enrolled_students_count = serializers.SerializerMethodField()
    room_capacity = serializers.SerializerMethodField()

    class Meta:
        model = Lesson
        fields = ['id', 'course', 'course_name', 'room', 'room_name',
                  'instructor', 'instructor_name', 'day_of_week', 'day_name',
                  'start_time', 'end_time', 'lesson_date', 'price', 'lesson_price_override',
                  'additional_course_prices', 'instructor_salary_override', 'is_recurring',
                  'status', 'notes', 'enrolled_students_count', 'room_capacity',
                  'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_additional_course_prices(self, value):
        return _normalize_additional_course_prices(value)
    
    def get_day_name(self, obj):
        """Convert day number to Hebrew name"""
        days = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']
        return days[obj.day_of_week] if 0 <= obj.day_of_week < 7 else ''
    
    def get_enrolled_students_count(self, obj):
        """Count enrolled students for this lesson"""
        return LessonEnrollment.objects.filter(lesson=obj, status='active').count()
    
    def get_room_capacity(self, obj):
        """Get room capacity for this lesson"""
        return obj.room.capacity if obj.room else None
    
    def validate(self, data):
        """Validate that room and instructor are available during the requested time"""
        # Branch now lives on the course, not the lesson
        course = data.get('course') or (self.instance.course if self.instance else None)
        branch = course.branch if course else None
        room = data.get('room') or (self.instance.room if self.instance else None)
        instructor = data.get('instructor') or (self.instance.instructor if self.instance else None)
        day_of_week = data.get('day_of_week', self.instance.day_of_week if self.instance else None)
        start_time = data.get('start_time', self.instance.start_time if self.instance else None)
        end_time = data.get('end_time', self.instance.end_time if self.instance else None)
        lesson_is_recurring = data.get('is_recurring', self.instance.is_recurring if self.instance else True)
        lesson_date = data.get('lesson_date', self.instance.lesson_date if self.instance else None)

        if not all([day_of_week is not None, start_time, end_time]):
            return data

        # Base query for conflicting lessons (same day and overlapping time)
        base_conflict_query = Q(
            day_of_week=day_of_week,
            status='scheduled',
        ) & ~(Q(end_time__lte=start_time) | Q(start_time__gte=end_time))

        # Exclude current instance if updating
        exclude_query = Q(pk=self.instance.pk) if self.instance else Q(pk=None)

        # Check for room conflicts (if room is specified)
        if branch and room:
            room_conflicts = Lesson.objects.filter(
                base_conflict_query,
                course__branch=branch,
                room=room,
            ).exclude(exclude_query)

            if room_conflicts.exists():
                conflict = room_conflicts.first()
                raise serializers.ValidationError({
                    'room': f'החדר תפוס ביום זה בין השעות {conflict.start_time.strftime("%H:%M")} - {conflict.end_time.strftime("%H:%M")}'
                })

            if timed_event_conflicts_lesson(
                branch,
                room,
                day_of_week,
                start_time,
                end_time,
                lesson_is_recurring=lesson_is_recurring,
                lesson_date=lesson_date,
            ):
                raise serializers.ValidationError({
                    'room': 'החדר תפוס באותה שעה (אירוע או שכירות בלוח)'
                })

        # Check for instructor conflicts (if instructor is specified)
        if instructor:
            instructor_conflicts = Lesson.objects.filter(
                base_conflict_query,
                instructor=instructor,
            ).exclude(exclude_query)

            if instructor_conflicts.exists():
                conflict = instructor_conflicts.first()
                raise serializers.ValidationError({
                    'instructor': f'המדריך תפוס ביום זה בין השעות {conflict.start_time.strftime("%H:%M")} - {conflict.end_time.strftime("%H:%M")}'
                })

        return data


# Legacy serializers for backward compatibility
class CourseListSerializer(serializers.ModelSerializer):
    """Simple course list for dropdowns"""
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    course_type_name = serializers.CharField(source='course_type.name', read_only=True, allow_null=True)
    
    class Meta:
        model = Course
        fields = ['id', 'name', 'course_type_name', 'branch_name', 'price']


class LessonListSerializer(serializers.ModelSerializer):
    """Simple lesson list"""
    course_name = serializers.CharField(source='course.name', read_only=True)
    day_name = serializers.SerializerMethodField()
    
    class Meta:
        model = Lesson
        fields = ['id', 'course_name', 'day_of_week', 'day_name', 'start_time', 'end_time']
    
    def get_day_name(self, obj):
        """Convert day number to Hebrew name"""
        days = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']
        return days[obj.day_of_week] if 0 <= obj.day_of_week < 7 else ''
