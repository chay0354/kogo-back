from decimal import Decimal, InvalidOperation

from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from apps.courses.models import CourseType, Course, Lesson, LessonBundle
from apps.enrollments.models import LessonEnrollment
from apps.enrollments.enrollment_counts import count_distinct_paying_children, count_paying_enrollments, is_paying_enrollment
from apps.core.models import Branch, Room, UserProfile
from apps.core.scoping import scope_courses
from apps.instructors.models import Instructor
from apps.scheduling.studio_conflict import timed_event_conflicts_lesson


User = get_user_model()


def _scoped_courses_for(serializer, base_qs):
    """Limit a Course queryset to courses visible to the requesting user."""
    request = serializer.context.get('request')
    user = getattr(request, 'user', None)
    if user is not None:
        return scope_courses(base_qs, user)
    return base_qs


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
        """Count active courses under this course type (scoped to the manager)."""
        annotated = getattr(obj, 'courses_count', None)
        if annotated is not None:
            return annotated
        return _scoped_courses_for(
            self, obj.courses.filter(is_active=True)
        ).count()
    
    def get_lessons_count(self, obj):
        """Count recurring lessons under this course type (scoped to the manager)."""
        annotated = getattr(obj, 'lessons_count', None)
        if annotated is not None:
            return annotated
        courses = _scoped_courses_for(self, obj.courses.all())
        return Lesson.objects.filter(
            course__in=courses,
            is_recurring=True
        ).count()
    
    def get_students_count(self, obj):
        """Count paying enrolled students under this course type (scoped)."""
        annotated = getattr(obj, 'students_count', None)
        if annotated is not None:
            return annotated
        courses = _scoped_courses_for(self, obj.courses.all())
        return count_paying_enrollments(courses=courses)
    
    def get_branches(self, obj):
        """Get distinct branches that host (visible) courses of this course type."""
        branches_by_id = {}
        for course in obj.courses.all():
            if course.branch_id and course.is_active:
                branches_by_id[str(course.branch_id)] = course.branch
        if branches_by_id:
            return BranchMinimalSerializer(branches_by_id.values(), many=True).data
        courses = _scoped_courses_for(self, obj.courses.all())
        branches = Branch.objects.filter(
            courses__in=courses
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
        """Paying active enrollments only (trial test signups excluded)."""
        counts = self.context.get('paying_enrollment_counts')
        if counts is not None:
            return counts.get(obj.id, 0)
        if hasattr(obj, '_prefetched_objects_cache') and 'enrollments' in obj._prefetched_objects_cache:
            return sum(1 for e in obj.enrollments.all() if is_paying_enrollment(e))
        return count_paying_enrollments(lesson=obj)
    
    def get_total_students_count(self, obj):
        """Get count of all enrollments regardless of status (for student count display)"""
        counts = self.context.get('total_enrollment_counts')
        if counts is not None:
            return counts.get(obj.id, 0)
        if hasattr(obj, '_prefetched_objects_cache') and 'enrollments' in obj._prefetched_objects_cache:
            return sum(1 for e in obj.enrollments.all() if e.status == 'active')
        return obj.enrollments.filter(status='active').count()
    
    def get_day_name(self, obj):
        """Convert day number to Hebrew name"""
        days = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']
        return days[obj.day_of_week] if 0 <= obj.day_of_week < 7 else ''


class CourseWithLessonsSerializer(serializers.ModelSerializer):
    """Course with nested lessons for details view"""
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    instructor = InstructorMinimalSerializer(read_only=True)
    lessons = LessonWithEnrollmentsSerializer(many=True, read_only=True)
    course_enrollment_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Course
        fields = ['id', 'name', 'description', 'price', 'capacity',
                  'min_age', 'max_age', 'is_adult', 'must_attend_all_lessons',
                  'trial_lesson_is_paid', 'trial_lesson_price',
                  'branch', 'branch_name', 'instructor', 'instructor_salary_override',
                  'external_link', 'lessons', 'course_enrollment_count', 'is_active']

    def get_course_enrollment_count(self, obj):
        """
        Distinct active students in this course (paying + trial), across all lessons.
        Used so every lesson row and the course header show the same enrollment figure.
        """
        counts = self.context.get('course_enrollment_counts')
        if counts is not None:
            return counts.get(obj.id, 0)
        child_ids = set()
        for lesson in obj.lessons.all():
            enrollments = (
                lesson.enrollments.all()
                if hasattr(lesson, '_prefetched_objects_cache') and 'enrollments' in lesson._prefetched_objects_cache
                else lesson.enrollments.filter(status='active')
            )
            for enrollment in enrollments:
                if enrollment.status == 'active':
                    child_ids.add(enrollment.child_id)
        return len(child_ids)


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
    managers = serializers.PrimaryKeyRelatedField(
        many=True,
        required=False,
        queryset=User.objects.filter(profile__role=UserProfile.ROLE_MANAGER),
    )
    managers_detail = serializers.SerializerMethodField()
    instructor = serializers.PrimaryKeyRelatedField(
        queryset=Instructor.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
    instructor_detail = InstructorMinimalSerializer(source='instructor', read_only=True)
    
    class Meta:
        model = Course
        fields = ['id', 'course_type', 'course_type_name', 'name', 'description',
                  'price', 'capacity', 'branch', 'branch_name',
                  'min_age', 'max_age', 'is_active', 'is_adult', 'must_attend_all_lessons',
                  'trial_lesson_is_paid', 'trial_lesson_price',
                  'external_link', 'lessons_count', 'enrolled_students_count',
                  'lessons', 'managers', 'managers_detail', 'instructor', 'instructor_detail',
                  'instructor_salary_override', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate(self, attrs):
        instance = getattr(self, 'instance', None)
        is_paid = attrs.get(
            'trial_lesson_is_paid',
            instance.trial_lesson_is_paid if instance else False,
        )
        price = attrs.get(
            'trial_lesson_price',
            instance.trial_lesson_price if instance else None,
        )
        if is_paid and (price is None or price <= 0):
            raise serializers.ValidationError({
                'trial_lesson_price': 'יש להזין מחיר גדול מ-0 לשיעור ניסיון בתשלום',
            })
        if not is_paid:
            attrs['trial_lesson_price'] = None
        return attrs

    def get_managers_detail(self, obj):
        """Lightweight list of assigned managers for display in the course dialog."""
        return [
            {
                'id': str(u.id),
                'name': (f"{u.first_name} {u.last_name}".strip() or u.email),
                'email': u.email,
            }
            for u in obj.managers.all()
        ]
    
    def get_lessons_count(self, obj):
        """Count lessons for this course (batched in list view via context)."""
        counts = self.context.get('lessons_counts')
        if counts is not None:
            return counts.get(obj.id, 0)
        return obj.lessons.filter(status='scheduled').count()
    
    def get_enrolled_students_count(self, obj):
        """Count distinct paying children across all lessons of this course."""
        counts = self.context.get('paying_children_counts')
        if counts is not None:
            return counts.get(obj.id, 0)
        return count_distinct_paying_children(course=obj)
    
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

    def create(self, validated_data):
        course = super().create(validated_data)
        course.sync_instructor_to_lessons()
        return course

    def update(self, instance, validated_data):
        previous_instructor = instance.instructor
        course = super().update(instance, validated_data)
        if 'instructor' in validated_data and previous_instructor != course.instructor:
            course.sync_instructor_to_lessons(previous_instructor=previous_instructor)
        return course


class LessonSerializer(serializers.ModelSerializer):
    """Basic Lesson serializer for CRUD operations"""
    course_name = serializers.CharField(source='course.name', read_only=True)
    room_name = serializers.CharField(source='room.name', read_only=True, allow_null=True)
    instructor = serializers.PrimaryKeyRelatedField(
        queryset=Instructor.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
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
        read_only_fields = ['id', 'instructor_salary_override', 'created_at', 'updated_at']

    def create(self, validated_data):
        if validated_data.get('instructor') is None:
            course = validated_data.get('course')
            if course is not None:
                validated_data['instructor'] = course.instructor
        return super().create(validated_data)

    def validate_additional_course_prices(self, value):
        return _normalize_additional_course_prices(value)

    def validate_price(self, value):
        """Pricing is monthly at course level only; ignore per-lesson price."""
        return None

    def get_day_name(self, obj):
        """Convert day number to Hebrew name"""
        days = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']
        return days[obj.day_of_week] if 0 <= obj.day_of_week < 7 else ''
    
    def get_enrolled_students_count(self, obj):
        """Count paying enrollments for this lesson (batched in list view via context)."""
        counts = self.context.get('paying_enrollment_counts')
        if counts is not None:
            return counts.get(obj.id, 0)
        return count_paying_enrollments(lesson=obj)
    
    def get_room_capacity(self, obj):
        """Get room capacity for this lesson"""
        return obj.room.capacity if obj.room else None
    
    def validate(self, data):
        """Validate that room and instructor are available during the requested time"""
        # Branch now lives on the course, not the lesson
        course = data.get('course') or (self.instance.course if self.instance else None)
        branch = course.branch if course else None
        room = data.get('room') or (self.instance.room if self.instance else None)
        if 'instructor' in data:
            instructor = data.get('instructor')
        elif self.instance:
            instructor = self.instance.instructor
        else:
            instructor = course.instructor if course else None
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


class LessonBundleLessonSerializer(serializers.ModelSerializer):
    """Minimal lesson info nested inside a bundle (schedule, instructor, studio)."""
    day_name = serializers.SerializerMethodField()
    instructor_id = serializers.UUIDField(read_only=True, allow_null=True)
    instructor_name = serializers.CharField(source='instructor.full_name', read_only=True, allow_null=True)
    room_id = serializers.UUIDField(read_only=True, allow_null=True)
    room_name = serializers.CharField(source='room.name', read_only=True, allow_null=True)

    class Meta:
        model = Lesson
        fields = [
            'id', 'day_of_week', 'day_name', 'start_time', 'end_time',
            'instructor_id', 'instructor_name', 'room_id', 'room_name',
        ]

    def get_day_name(self, obj):
        days = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']
        return days[obj.day_of_week] if 0 <= obj.day_of_week < 7 else ''


class LessonBundleSerializer(serializers.ModelSerializer):
    """CRUD serializer for LessonBundle — combined-price packages of a course's own lessons."""
    lessons_detail = LessonBundleLessonSerializer(source='lessons', many=True, read_only=True)
    price_per_lesson = serializers.SerializerMethodField()
    lesson_instructors = serializers.DictField(
        child=serializers.UUIDField(allow_null=True),
        required=False,
        write_only=True,
    )
    lesson_rooms = serializers.DictField(
        child=serializers.UUIDField(allow_null=True),
        required=False,
        write_only=True,
    )

    class Meta:
        model = LessonBundle
        fields = [
            'id', 'course', 'name', 'lessons', 'lessons_detail',
            'combined_price', 'price_per_lesson', 'is_active',
            'lesson_instructors', 'lesson_rooms', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_price_per_lesson(self, obj):
        return obj.price_per_lesson()

    def validate(self, data):
        course = data.get('course') or (self.instance.course if self.instance else None)
        lessons = data.get('lessons')
        if lessons is None and self.instance is not None:
            lessons = list(self.instance.lessons.all())

        if lessons is not None:
            if len(lessons) < 2:
                raise serializers.ValidationError({'lessons': 'יש לבחור לפחות 2 שיעורים'})
            outside_course = [l for l in lessons if course and l.course_id != course.id]
            if outside_course:
                raise serializers.ValidationError({'lessons': 'כל השיעורים במסלול חייבים להשתייך לאותו חוג'})

        combined_price = data.get('combined_price', getattr(self.instance, 'combined_price', None))
        if combined_price is not None and combined_price < 0:
            raise serializers.ValidationError({'combined_price': 'המחיר לא יכול להיות שלילי'})

        selected = lessons or []
        self._validate_instructor_mapping(data.get('lesson_instructors'), selected)
        self._validate_room_mapping(data.get('lesson_rooms'), selected, course)

        return data

    def _validate_instructor_mapping(self, mapping, selected):
        if not mapping:
            return
        lesson_ids = {str(l.id) for l in selected}
        if any(key not in lesson_ids for key in mapping):
            raise serializers.ValidationError({
                'lesson_instructors': 'ניתן לשנות מדריך רק לשיעורים שבמסלול',
            })
        for lesson_id, instructor_id in mapping.items():
            if not instructor_id:
                continue
            instructor = Instructor.objects.filter(pk=instructor_id, is_active=True).first()
            if instructor is None:
                raise serializers.ValidationError({
                    'lesson_instructors': 'המדריך שנבחר אינו קיים',
                })
            lesson = next((l for l in selected if str(l.id) == str(lesson_id)), None)
            if lesson is None:
                continue
            conflict_message = self._instructor_busy_message(lesson, instructor)
            if conflict_message:
                raise serializers.ValidationError({'lesson_instructors': conflict_message})

    def _validate_room_mapping(self, mapping, selected, course):
        if not mapping:
            return
        lesson_ids = {str(l.id) for l in selected}
        if any(key not in lesson_ids for key in mapping):
            raise serializers.ValidationError({
                'lesson_rooms': 'ניתן לשנות סטודיו רק לשיעורים שבמסלול',
            })
        branch = course.branch if course else None
        for lesson_id, room_id in mapping.items():
            if not room_id:
                continue
            room = Room.objects.filter(pk=room_id, is_active=True).first()
            if room is None:
                raise serializers.ValidationError({
                    'lesson_rooms': 'הסטודיו שנבחר אינו קיים',
                })
            if branch and room.branch_id != branch.id:
                raise serializers.ValidationError({
                    'lesson_rooms': 'הסטודיו חייב להיות בסניף של החוג',
                })
            lesson = next((l for l in selected if str(l.id) == str(lesson_id)), None)
            if lesson is None:
                continue
            conflict_message = self._room_busy_message(lesson, room)
            if conflict_message:
                raise serializers.ValidationError({'lesson_rooms': conflict_message})
            if timed_event_conflicts_lesson(
                branch,
                room,
                lesson.day_of_week,
                lesson.start_time,
                lesson.end_time,
                lesson_is_recurring=lesson.is_recurring,
                lesson_date=lesson.lesson_date,
            ):
                raise serializers.ValidationError({
                    'lesson_rooms': 'החדר תפוס באותה שעה (אירוע או שכירות בלוח)',
                })

    def create(self, validated_data):
        instructor_mapping = validated_data.pop('lesson_instructors', None)
        room_mapping = validated_data.pop('lesson_rooms', None)
        with transaction.atomic():
            bundle = super().create(validated_data)
            self._apply_lesson_instructors(bundle, instructor_mapping)
            self._apply_lesson_rooms(bundle, room_mapping)
            return bundle

    def update(self, instance, validated_data):
        instructor_mapping = validated_data.pop('lesson_instructors', None)
        room_mapping = validated_data.pop('lesson_rooms', None)
        with transaction.atomic():
            bundle = super().update(instance, validated_data)
            self._apply_lesson_instructors(bundle, instructor_mapping)
            self._apply_lesson_rooms(bundle, room_mapping)
            return bundle

    def _apply_lesson_instructors(self, bundle, mapping):
        if not mapping:
            return
        for lesson_id, instructor_id in mapping.items():
            lesson = Lesson.objects.filter(pk=lesson_id, course_id=bundle.course_id).first()
            if lesson is None:
                continue
            lesson.instructor_id = instructor_id
            lesson.save(update_fields=['instructor'])

    def _apply_lesson_rooms(self, bundle, mapping):
        if not mapping:
            return
        for lesson_id, room_id in mapping.items():
            lesson = Lesson.objects.filter(pk=lesson_id, course_id=bundle.course_id).first()
            if lesson is None:
                continue
            lesson.room_id = room_id
            lesson.save(update_fields=['room'])

    @staticmethod
    def _overlap_query(lesson):
        return Q(
            day_of_week=lesson.day_of_week,
            status='scheduled',
        ) & ~(Q(end_time__lte=lesson.start_time) | Q(start_time__gte=lesson.end_time))

    @classmethod
    def _instructor_busy_message(cls, lesson, instructor):
        conflict = (
            Lesson.objects.filter(cls._overlap_query(lesson), instructor=instructor)
            .exclude(pk=lesson.pk)
            .first()
        )
        if not conflict:
            return None
        return cls._busy_slot_message('המדריך', lesson, conflict)

    @classmethod
    def _room_busy_message(cls, lesson, room):
        conflict = (
            Lesson.objects.filter(cls._overlap_query(lesson), room=room)
            .exclude(pk=lesson.pk)
            .first()
        )
        if not conflict:
            return None
        return cls._busy_slot_message('החדר', lesson, conflict)

    @staticmethod
    def _busy_slot_message(subject, lesson, conflict):
        days = ['ראשון', 'שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת']
        day_name = days[lesson.day_of_week] if 0 <= lesson.day_of_week < 7 else ''
        return (
            f'{subject} תפוס ביום {day_name} בין השעות '
            f'{conflict.start_time.strftime("%H:%M")} - {conflict.end_time.strftime("%H:%M")}'
        )


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
