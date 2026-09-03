from django.test import TestCase

from apps.core.models import Branch, City, Room
from apps.courses.models import Course, CourseType, Lesson, LessonBundle
from apps.instructors.models import Instructor, InstructorPairPhoto
from apps.instructors.pair_photos import (
    combined_tracks,
    pair_photo_for_lessons,
    pair_photo_map,
    partners_for_instructor,
)


class PairPhotoTests(TestCase):
    """A track taught by two instructors carries one photo, shared by both."""

    def setUp(self):
        city = City.objects.create(name='פתח תקווה')
        self.branch = Branch.objects.create(name='מרכז העיר', city=city)
        self.room = Room.objects.create(name='אולם', branch=self.branch, capacity=20)
        course_type = CourseType.objects.create(name='קפוארה')
        self.ido = Instructor.objects.create(first_name='עידו', last_name='לוי', phone='0501111111')
        self.noa = Instructor.objects.create(first_name='נועה', last_name='ברק', phone='0502222222')
        self.course = Course.objects.create(
            name='קפוארה ואקרובטיקה', course_type=course_type, branch=self.branch,
            price=300, capacity=20,
        )
        self.monday = Lesson.objects.create(
            course=self.course, room=self.room, instructor=self.ido,
            day_of_week=1, start_time='16:00', end_time='16:45',
        )
        self.thursday = Lesson.objects.create(
            course=self.course, room=self.room, instructor=self.noa,
            day_of_week=4, start_time='16:00', end_time='16:45',
        )
        self.bundle = LessonBundle.objects.create(
            course=self.course, name='פעמיים בשבוע', combined_price=300,
        )
        self.bundle.lessons.set([self.monday, self.thursday])

    def test_a_track_with_two_instructors_is_found(self):
        tracks = combined_tracks()
        self.assertEqual(len(tracks), 1)
        self.assertEqual({person.id for person in tracks[0]['instructors']}, {self.ido.id, self.noa.id})

    def test_a_track_taught_by_one_instructor_is_not_a_pair(self):
        self.thursday.instructor = self.ido
        self.thursday.save(update_fields=['instructor'])
        self.assertEqual(combined_tracks(), [])

    def test_both_instructors_see_the_very_same_photo(self):
        first, second = InstructorPairPhoto.ordered_pair(self.ido, self.noa)
        InstructorPairPhoto.objects.create(
            first_instructor=first, second_instructor=second, photo_url='https://cdn/pair.jpg',
        )

        from_ido = partners_for_instructor(self.ido)
        from_noa = partners_for_instructor(self.noa)

        self.assertEqual(len(from_ido), 1)
        self.assertEqual(from_ido[0]['partner_name'], self.noa.full_name)
        self.assertEqual(from_ido[0]['photo_url'], 'https://cdn/pair.jpg')
        self.assertEqual(from_noa[0]['partner_name'], self.ido.full_name)
        self.assertEqual(from_noa[0]['photo_url'], from_ido[0]['photo_url'])

    def test_the_pair_is_one_row_whichever_way_round_it_is_written(self):
        first, second = InstructorPairPhoto.ordered_pair(self.noa, self.ido)
        InstructorPairPhoto.objects.create(
            first_instructor=first, second_instructor=second, photo_url='https://cdn/pair.jpg',
        )
        self.assertIsNotNone(InstructorPairPhoto.for_pair(self.ido, self.noa))
        self.assertIsNotNone(InstructorPairPhoto.for_pair(self.noa, self.ido))

    def test_the_widget_gets_the_pair_photo_for_the_track(self):
        first, second = InstructorPairPhoto.ordered_pair(self.ido, self.noa)
        InstructorPairPhoto.objects.create(
            first_instructor=first, second_instructor=second, photo_url='https://cdn/pair.jpg',
        )
        lessons = list(self.bundle.lessons.all())
        self.assertEqual(pair_photo_for_lessons(lessons), 'https://cdn/pair.jpg')
        self.assertEqual(pair_photo_for_lessons(lessons, pair_photo_map()), 'https://cdn/pair.jpg')

    def test_a_track_with_no_uploaded_photo_stays_empty(self):
        self.assertIsNone(pair_photo_for_lessons(list(self.bundle.lessons.all())))
        self.assertIsNone(partners_for_instructor(self.ido)[0]['photo_url'])
