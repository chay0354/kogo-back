"""
Unit tests for Courses app models.

Tests coverage:
- CourseType: creation, ordering
- Course: price, capacity, age restrictions
- Lesson: day_of_week, time slots, price override, has_occurred method
"""
from decimal import Decimal
from datetime import date, time
from django.test import TestCase
from django.utils import timezone

from apps.core.tests.test_fixtures import TestDataFactory
from apps.courses.models import CourseType, Course, Lesson


class CourseTypeModelTest(TestCase):
    """Test CourseType model"""
    
    def test_create_course_type(self):
        """Test creating a course type"""
        course_type = CourseType.objects.create(
            name="קפואירה",
            description="אמנות לחימה ברזילאית"
        )
        
        self.assertEqual(course_type.name, "קפואירה")
        self.assertEqual(course_type.description, "אמנות לחימה ברזילאית")
        self.assertTrue(course_type.is_active)
    
    def test_course_type_str_representation(self):
        """Test course type string representation"""
        course_type = CourseType.objects.create(name="כדורסל")
        self.assertEqual(str(course_type), "כדורסל")
    
    def test_course_type_is_active_flag(self):
        """Test course type can be deactivated"""
        course_type = CourseType.objects.create(
            name="ג'ודו",
            is_active=True
        )
        
        course_type.is_active = False
        course_type.save()
        course_type.refresh_from_db()
        
        self.assertFalse(course_type.is_active)
    
    def test_course_type_ordering(self):
        """Test course types are ordered by name"""
        CourseType.objects.create(name="זומבה")
        CourseType.objects.create(name="אקרובטיקה")
        CourseType.objects.create(name="נינג'ה")
        
        types = list(CourseType.objects.all())
        self.assertEqual(types[0].name, "אקרובטיקה")
        self.assertEqual(types[1].name, "זומבה")
        self.assertEqual(types[2].name, "נינג'ה")


class CourseModelTest(TestCase):
    """Test Course model"""
    
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
        self.course_type = TestDataFactory.create_course_type()
    
    def test_create_course(self):
        """Test creating a course"""
        course = Course.objects.create(
            course_type=self.course_type,
            name="מתחילים",
            description="חוג מתחילים לגילאי 6-8",
            price=Decimal('350.00'),
            capacity=20,
            branch=self.branch
        )
        
        self.assertEqual(course.name, "מתחילים")
        self.assertEqual(course.price, Decimal('350.00'))
        self.assertEqual(course.capacity, 20)
        self.assertTrue(course.is_active)
    
    def test_course_with_age_restrictions(self):
        """Test course with min and max age"""
        course = Course.objects.create(
            course_type=self.course_type,
            name="מתקדמים",
            price=Decimal('400.00'),
            capacity=15,
            branch=self.branch,
            min_age=10,
            max_age=14
        )
        
        self.assertEqual(course.min_age, 10)
        self.assertEqual(course.max_age, 14)
    
    def test_course_str_representation(self):
        """Test course string representation includes course type"""
        course = Course.objects.create(
            course_type=self.course_type,
            name="בינוניים",
            price=Decimal('375.00'),
            capacity=18,
            branch=self.branch
        )
        
        str_repr = str(course)
        self.assertIn(self.course_type.name, str_repr)
        self.assertIn("בינוניים", str_repr)
    
    def test_course_cascade_delete_with_course_type(self):
        """Test course is deleted when course type is deleted"""
        course = Course.objects.create(
            course_type=self.course_type,
            name="בוקר",
            price=Decimal('350.00'),
            capacity=20,
            branch=self.branch
        )
        
        course_id = course.id
        self.course_type.delete()
        
        with self.assertRaises(Course.DoesNotExist):
            Course.objects.get(id=course_id)
    
    def test_course_is_active_flag(self):
        """Test course can be deactivated"""
        course = Course.objects.create(
            course_type=self.course_type,
            name="ערב",
            price=Decimal('350.00'),
            capacity=20,
            branch=self.branch,
            is_active=True
        )
        
        course.is_active = False
        course.save()
        course.refresh_from_db()
        
        self.assertFalse(course.is_active)
    
    def test_course_ordering(self):
        """Test courses are ordered by course_type and name"""
        type2 = CourseType.objects.create(name="כדורסל")
        
        course1 = Course.objects.create(
            course_type=self.course_type,
            name="ב",
            price=Decimal('350.00'),
            capacity=20,
            branch=self.branch
        )
        
        course2 = Course.objects.create(
            course_type=self.course_type,
            name="א",
            price=Decimal('350.00'),
            capacity=20,
            branch=self.branch
        )
        
        course3 = Course.objects.create(
            course_type=type2,
            name="א",
            price=Decimal('400.00'),
            capacity=25,
            branch=self.branch
        )
        
        courses = list(Course.objects.all())
        # Should be ordered by course_type name, then course name
        self.assertEqual(courses[0], course3)  # כדורסל - א
        self.assertEqual(courses[1], course2)  # קפואירה - א
        self.assertEqual(courses[2], course1)  # קפואירה - ב


class LessonModelTest(TestCase):
    """Test Lesson model"""
    
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
        self.room = TestDataFactory.create_room(branch=self.branch)
        self.course = TestDataFactory.create_course(branch=self.branch)
        self.instructor = TestDataFactory.create_instructor(branch=self.branch)
    
    def test_create_lesson(self):
        """Test creating a lesson"""
        lesson = Lesson.objects.create(
            course=self.course,
            room=self.room,
            instructor=self.instructor,
            day_of_week=0,  # Sunday
            start_time=time(16, 0),
            end_time=time(17, 0),
            status='scheduled'
        )
        
        self.assertEqual(lesson.day_of_week, 0)
        self.assertEqual(lesson.start_time, time(16, 0))
        self.assertEqual(lesson.end_time, time(17, 0))
        self.assertEqual(lesson.status, 'scheduled')
        self.assertTrue(lesson.is_recurring)
    
    def test_lesson_day_of_week_choices(self):
        """Test lesson day_of_week choices (0-6)"""
        for day in range(7):
            lesson = Lesson.objects.create(
                course=self.course,
                    day_of_week=day,
                start_time=time(16, 0),
                end_time=time(17, 0)
            )
            self.assertEqual(lesson.day_of_week, day)
    
    def test_lesson_status_choices(self):
        """Test lesson status choices"""
        statuses = ['scheduled', 'completed', 'cancelled']
        
        for status in statuses:
            lesson = Lesson.objects.create(
                course=self.course,
                    day_of_week=0,
                start_time=time(16, 0),
                end_time=time(17, 0),
                status=status
            )
            self.assertEqual(lesson.status, status)
    
    def test_lesson_with_price_override(self):
        """Test lesson with price override (different from course price)"""
        lesson = Lesson.objects.create(
            course=self.course,
            day_of_week=1,
            start_time=time(17, 0),
            end_time=time(18, 0),
            price=Decimal('400.00'),  # Override course price
            lesson_price_override=Decimal('375.00')
        )
        
        self.assertEqual(lesson.price, Decimal('400.00'))
        self.assertEqual(lesson.lesson_price_override, Decimal('375.00'))
    
    def test_lesson_with_instructor_salary_override(self):
        """Test lesson with instructor salary override"""
        lesson = Lesson.objects.create(
            course=self.course,
            instructor=self.instructor,
            day_of_week=2,
            start_time=time(18, 0),
            end_time=time(19, 0),
            instructor_salary_override=Decimal('150.00')
        )
        
        self.assertEqual(lesson.instructor_salary_override, Decimal('150.00'))
    
    def test_lesson_with_specific_date(self):
        """Test lesson with specific lesson_date (for one-time lessons)"""
        lesson_date = date(2024, 3, 15)
        
        lesson = Lesson.objects.create(
            course=self.course,
            day_of_week=4,
            start_time=time(15, 0),
            end_time=time(16, 0),
            lesson_date=lesson_date,
            is_recurring=False
        )
        
        self.assertEqual(lesson.lesson_date, lesson_date)
        self.assertFalse(lesson.is_recurring)
    
    def test_lesson_cancellation(self):
        """Test lesson cancellation with reason and timestamp"""
        lesson = Lesson.objects.create(
            course=self.course,
            day_of_week=3,
            start_time=time(16, 0),
            end_time=time(17, 0),
            status='cancelled',
            cancellation_reason="מדריך חולה",
            cancelled_at=timezone.now()
        )
        
        self.assertEqual(lesson.status, 'cancelled')
        self.assertEqual(lesson.cancellation_reason, "מדריך חולה")
        self.assertIsNotNone(lesson.cancelled_at)
    
    def test_lesson_str_representation(self):
        """Test lesson string representation includes course, day, and time"""
        lesson = Lesson.objects.create(
            course=self.course,
            day_of_week=0,  # Sunday
            start_time=time(16, 30),
            end_time=time(17, 30)
        )
        
        str_repr = str(lesson)
        self.assertIn(self.course.name, str_repr)
        self.assertIn("16:30", str_repr)
    
    def test_lesson_has_occurred_method(self):
        """Test lesson has_occurred method for salary calculation"""
        # Lesson in the past
        past_lesson = Lesson.objects.create(
            course=self.course,
            day_of_week=0,
            start_time=time(16, 0),
            end_time=time(17, 0),
            status='completed',
            lesson_date=date.today() - timezone.timedelta(days=7)
        )
        
        self.assertTrue(past_lesson.has_occurred())
        
        # Lesson in the future
        future_lesson = Lesson.objects.create(
            course=self.course,
            day_of_week=1,
            start_time=time(16, 0),
            end_time=time(17, 0),
            status='scheduled',
            lesson_date=date.today() + timezone.timedelta(days=7)
        )
        
        self.assertFalse(future_lesson.has_occurred())
        
        # Cancelled lesson should not count
        cancelled_lesson = Lesson.objects.create(
            course=self.course,
            day_of_week=2,
            start_time=time(16, 0),
            end_time=time(17, 0),
            status='cancelled',
            lesson_date=date.today() - timezone.timedelta(days=3)
        )
        
        self.assertFalse(cancelled_lesson.has_occurred())
    
    def test_lesson_ordering(self):
        """Test lessons are ordered by day_of_week and start_time"""
        lesson1 = Lesson.objects.create(
            course=self.course,
            day_of_week=0,
            start_time=time(17, 0),
            end_time=time(18, 0)
        )
        
        lesson2 = Lesson.objects.create(
            course=self.course,
            day_of_week=0,
            start_time=time(16, 0),
            end_time=time(17, 0)
        )
        
        lesson3 = Lesson.objects.create(
            course=self.course,
            day_of_week=1,
            start_time=time(16, 0),
            end_time=time(17, 0)
        )
        
        lessons = list(Lesson.objects.all())
        self.assertEqual(lessons[0], lesson2)  # Sunday 16:00
        self.assertEqual(lessons[1], lesson1)  # Sunday 17:00
        self.assertEqual(lessons[2], lesson3)  # Monday 16:00
