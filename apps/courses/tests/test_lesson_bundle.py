"""
Unit tests for LessonBundle (combined "twice a week" package) — model,
serializer validation, and capacity behaviour.
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.core.tests.test_fixtures import TestDataFactory
from apps.courses.bundles import catalog_bundles_for_course, resolve_registration_bundle
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

    def test_inactive_must_attend_bundle_is_in_catalog_and_resolvable(self):
        self.course.must_attend_all_lessons = True
        self.course.save(update_fields=['must_attend_all_lessons'])
        bundle = LessonBundle.objects.create(
            course=self.course,
            combined_price=Decimal('300.00'),
            is_active=False,
        )
        bundle.lessons.set([self.lesson_a, self.lesson_b])

        catalog = catalog_bundles_for_course(self.course)
        self.assertEqual(catalog, [bundle])
        self.assertEqual(
            resolve_registration_bundle(course=self.course, bundle_id=str(bundle.id)),
            bundle,
        )

    def test_serializer_rejects_inverted_bundle_age_range(self):
        serializer = LessonBundleSerializer(data={
            'course': str(self.course.id),
            'name': 'פעמיים בשבוע',
            'lessons': [str(self.lesson_a.id), str(self.lesson_b.id)],
            'combined_price': '330.00',
            'min_age': 16,
            'max_age': 15,
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn('max_age', serializer.errors)

    def test_inactive_bundle_hidden_when_course_is_not_must_attend(self):
        bundle = LessonBundle.objects.create(
            course=self.course,
            combined_price=Decimal('300.00'),
            is_active=False,
        )
        bundle.lessons.set([self.lesson_a, self.lesson_b])

        self.assertEqual(catalog_bundles_for_course(self.course), [])
        self.assertIsNone(
            resolve_registration_bundle(course=self.course, bundle_id=str(bundle.id)),
        )


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


class LessonInstructorOverrideTest(TestCase):
    """Per-lesson instructor can differ from the course default (combined tracks)."""

    def setUp(self):
        self.instructor_a = TestDataFactory.create_instructor(first_name='Ava', last_name='Alpha')
        self.instructor_b = TestDataFactory.create_instructor(first_name='Ben', last_name='Beta')
        self.instructor_c = TestDataFactory.create_instructor(first_name='Cara', last_name='Cohen')
        self.course = TestDataFactory.create_course(instructor=self.instructor_a)
        self.lesson_a = TestDataFactory.create_lesson(
            course=self.course, instructor=self.instructor_a, day_of_week=0,
        )
        self.lesson_b = TestDataFactory.create_lesson(
            course=self.course, instructor=self.instructor_a, day_of_week=3,
        )

    def test_lesson_serializer_updates_instructor(self):
        from apps.courses.serializers import LessonSerializer

        serializer = LessonSerializer(
            self.lesson_b,
            data={'instructor': str(self.instructor_b.id)},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.lesson_b.refresh_from_db()
        self.assertEqual(self.lesson_b.instructor_id, self.instructor_b.id)

    def test_changing_course_instructor_keeps_overridden_lessons(self):
        from apps.courses.serializers import CourseSerializer

        self.lesson_b.instructor = self.instructor_b
        self.lesson_b.save(update_fields=['instructor'])

        serializer = CourseSerializer(
            self.course,
            data={'instructor': str(self.instructor_c.id)},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        self.lesson_a.refresh_from_db()
        self.lesson_b.refresh_from_db()
        self.assertEqual(self.lesson_a.instructor_id, self.instructor_c.id)
        self.assertEqual(self.lesson_b.instructor_id, self.instructor_b.id)

    def test_bundle_save_assigns_per_lesson_instructors(self):
        serializer = LessonBundleSerializer(data={
            'course': str(self.course.id),
            'name': 'פעמיים בשבוע',
            'lessons': [str(self.lesson_a.id), str(self.lesson_b.id)],
            'combined_price': '300.00',
            'lesson_instructors': {
                str(self.lesson_a.id): str(self.instructor_a.id),
                str(self.lesson_b.id): str(self.instructor_b.id),
            },
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        bundle = serializer.save()

        self.lesson_a.refresh_from_db()
        self.lesson_b.refresh_from_db()
        self.assertEqual(self.lesson_a.instructor_id, self.instructor_a.id)
        self.assertEqual(self.lesson_b.instructor_id, self.instructor_b.id)

        detail = {str(row['id']): row for row in LessonBundleSerializer(bundle).data['lessons_detail']}
        self.assertEqual(detail[str(self.lesson_b.id)]['instructor_name'], self.instructor_b.full_name)

    def test_bundle_rejects_busy_instructor(self):
        other_course = TestDataFactory.create_course(name='קבוצה אחרת')
        TestDataFactory.create_lesson(
            course=other_course,
            instructor=self.instructor_b,
            day_of_week=3,
            start_time=self.lesson_b.start_time,
            end_time=self.lesson_b.end_time,
        )

        serializer = LessonBundleSerializer(data={
            'course': str(self.course.id),
            'lessons': [str(self.lesson_a.id), str(self.lesson_b.id)],
            'combined_price': '300.00',
            'lesson_instructors': {
                str(self.lesson_b.id): str(self.instructor_b.id),
            },
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn('lesson_instructors', serializer.errors)

    def test_bundle_save_assigns_per_lesson_rooms(self):
        room_b = TestDataFactory.create_room(branch=self.course.branch, name='סטודיו ב')
        serializer = LessonBundleSerializer(data={
            'course': str(self.course.id),
            'name': 'פעמיים בשבוע',
            'lessons': [str(self.lesson_a.id), str(self.lesson_b.id)],
            'combined_price': '300.00',
            'lesson_rooms': {
                str(self.lesson_a.id): str(self.lesson_a.room_id),
                str(self.lesson_b.id): str(room_b.id),
            },
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        bundle = serializer.save()

        self.lesson_b.refresh_from_db()
        self.assertEqual(self.lesson_b.room_id, room_b.id)
        detail = {str(row['id']): row for row in LessonBundleSerializer(bundle).data['lessons_detail']}
        self.assertEqual(detail[str(self.lesson_b.id)]['room_name'], room_b.name)

    def test_bundle_rejects_busy_room(self):
        busy_room = TestDataFactory.create_room(branch=self.course.branch, name='תפוס')
        other_course = TestDataFactory.create_course(branch=self.course.branch, name='קבוצה אחרת')
        TestDataFactory.create_lesson(
            course=other_course,
            room=busy_room,
            day_of_week=3,
            start_time=self.lesson_b.start_time,
            end_time=self.lesson_b.end_time,
        )

        serializer = LessonBundleSerializer(data={
            'course': str(self.course.id),
            'lessons': [str(self.lesson_a.id), str(self.lesson_b.id)],
            'combined_price': '300.00',
            'lesson_rooms': {
                str(self.lesson_b.id): str(busy_room.id),
            },
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn('lesson_rooms', serializer.errors)

    def test_bundle_rejects_room_from_another_branch(self):
        other_room = TestDataFactory.create_room(name='סניף אחר')
        serializer = LessonBundleSerializer(data={
            'course': str(self.course.id),
            'lessons': [str(self.lesson_a.id), str(self.lesson_b.id)],
            'combined_price': '300.00',
            'lesson_rooms': {
                str(self.lesson_b.id): str(other_room.id),
            },
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn('lesson_rooms', serializer.errors)
