"""
Unit tests for LessonBundle (combined "twice a week" package) — model,
serializer validation, and capacity behaviour.
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.core.tests.test_fixtures import TestDataFactory
from apps.courses.models import LessonBundle
from apps.courses.serializers import LessonBundleSerializer
from apps.enrollments.models import LessonEnrollment


class LessonBundleModelTest(TestCase):
    def setUp(self):
        self.course = TestDataFactory.create_course()
        self.lesson_a = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.lesson_b = TestDataFactory.create_lesson(course=self.course, day_of_week=3)

    def test_price_per_lesson_splits_evenly(self):
        bundle = LessonBundle.objects.create(course=self.course, combined_price=Decimal('300.00'))
        bundle.lessons.set([self.lesson_a, self.lesson_b])

        self.assertEqual(bundle.price_per_lesson(), Decimal('150.00'))

    def test_price_per_lesson_with_no_lessons_returns_combined_price(self):
        bundle = LessonBundle.objects.create(course=self.course, combined_price=Decimal('300.00'))
        self.assertEqual(bundle.price_per_lesson(), Decimal('300.00'))

    def test_str_falls_back_to_course_name(self):
        bundle = LessonBundle.objects.create(course=self.course, combined_price=Decimal('300.00'))
        self.assertIn(self.course.name, str(bundle))


class LessonBundleSerializerTest(TestCase):
    def setUp(self):
        self.course = TestDataFactory.create_course()
        self.other_course = TestDataFactory.create_course(name="קבוצה אחרת")
        self.lesson_a = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.lesson_b = TestDataFactory.create_lesson(course=self.course, day_of_week=3)
        self.lesson_other_course = TestDataFactory.create_lesson(course=self.other_course, day_of_week=1)

    def test_valid_bundle_creates_successfully(self):
        serializer = LessonBundleSerializer(data={
            'course': str(self.course.id),
            'name': 'פעמיים בשבוע',
            'lessons': [str(self.lesson_a.id), str(self.lesson_b.id)],
            'combined_price': '300.00',
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        bundle = serializer.save()
        self.assertEqual(bundle.lessons.count(), 2)
        self.assertEqual(bundle.price_per_lesson(), Decimal('150.00'))

    def test_rejects_fewer_than_two_lessons(self):
        serializer = LessonBundleSerializer(data={
            'course': str(self.course.id),
            'lessons': [str(self.lesson_a.id)],
            'combined_price': '300.00',
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn('lessons', serializer.errors)

    def test_rejects_lesson_from_a_different_course(self):
        serializer = LessonBundleSerializer(data={
            'course': str(self.course.id),
            'lessons': [str(self.lesson_a.id), str(self.lesson_other_course.id)],
            'combined_price': '300.00',
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn('lessons', serializer.errors)

    def test_rejects_negative_price(self):
        serializer = LessonBundleSerializer(data={
            'course': str(self.course.id),
            'lessons': [str(self.lesson_a.id), str(self.lesson_b.id)],
            'combined_price': '-10.00',
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn('combined_price', serializer.errors)


class LessonEnrollmentBundleValidationTest(TestCase):
    """LessonEnrollmentSerializer.validate() cross-checks bundle membership."""

    def setUp(self):
        from apps.enrollments.serializers import LessonEnrollmentSerializer
        self.LessonEnrollmentSerializer = LessonEnrollmentSerializer

        self.course = TestDataFactory.create_course()
        self.lesson_a = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.lesson_b = TestDataFactory.create_lesson(course=self.course, day_of_week=3)
        self.unrelated_lesson = TestDataFactory.create_lesson(day_of_week=1)
        self.child = TestDataFactory.create_child()

        self.bundle = LessonBundle.objects.create(course=self.course, combined_price=Decimal('300.00'))
        self.bundle.lessons.set([self.lesson_a, self.lesson_b])

    def test_enrollment_accepts_bundle_when_lesson_is_a_member(self):
        serializer = self.LessonEnrollmentSerializer(data={
            'lesson': str(self.lesson_a.id),
            'child': str(self.child.id),
            'bundle': str(self.bundle.id),
            'status': 'active',
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_enrollment_rejects_bundle_when_lesson_is_not_a_member(self):
        serializer = self.LessonEnrollmentSerializer(data={
            'lesson': str(self.unrelated_lesson.id),
            'child': str(self.child.id),
            'bundle': str(self.bundle.id),
            'status': 'active',
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn('bundle', serializer.errors)
