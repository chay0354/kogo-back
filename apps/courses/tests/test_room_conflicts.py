"""Studio overlap is a warning, not a save block."""
from datetime import time

from rest_framework import status

from apps.core.tests.test_fixtures import BaseAPITestCase, TestDataFactory
from apps.courses.serializers import LessonSerializer


class RoomConflictWarningTest(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.room = TestDataFactory.create_room(branch=self.branch, name='סטודיו 2')
        self.other = TestDataFactory.create_course(branch=self.branch, name='ו-ז-ח')
        self.ours = TestDataFactory.create_course(branch=self.branch, name='מסלול להקה')
        self.other_lesson = TestDataFactory.create_lesson(
            course=self.other,
            room=self.room,
            day_of_week=4,
            start_time=time(17, 0),
            end_time=time(18, 0),
        )
        self.our_lesson = TestDataFactory.create_lesson(
            course=self.ours,
            room=self.room,
            day_of_week=4,
            start_time=time(17, 0),
            end_time=time(18, 0),
        )

    def test_lesson_update_saves_when_studio_is_busy(self):
        serializer = LessonSerializer(
            self.our_lesson,
            data={'price': None, 'room': str(self.room.id)},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.our_lesson.refresh_from_db()
        self.assertEqual(self.our_lesson.room_id, self.room.id)

    def test_lesson_update_saves_when_instructor_is_busy(self):
        self.our_lesson.instructor = self.other_lesson.instructor
        self.our_lesson.save(update_fields=['instructor'])
        serializer = LessonSerializer(
            self.our_lesson,
            data={'price': None, 'instructor': str(self.other_lesson.instructor_id)},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

    def test_room_conflicts_lists_the_other_course(self):
        res = self.client.get(
            '/api/v1/courses/lessons/room-conflicts/',
            {
                'room': str(self.room.id),
                'day_of_week': 4,
                'start_time': '17:00',
                'end_time': '18:00',
                'exclude_course': str(self.ours.id),
            },
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        names = [row['name'] for row in res.data['conflicts']]
        self.assertIn('ו-ז-ח', names)
        self.assertNotIn('מסלול להקה', names)

    def test_room_conflicts_lists_instructor_overlap(self):
        res = self.client.get(
            '/api/v1/courses/lessons/room-conflicts/',
            {
                'instructor': str(self.other_lesson.instructor_id),
                'day_of_week': 4,
                'start_time': '17:00',
                'end_time': '18:00',
                'exclude_course': str(self.ours.id),
            },
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        kinds = {row['kind']: row['name'] for row in res.data['conflicts']}
        self.assertEqual(kinds.get('instructor'), 'ו-ז-ח')
