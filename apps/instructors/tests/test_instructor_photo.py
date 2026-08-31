"""Instructor photo: managers upload it to Supabase Storage, the public widget links to it."""
from io import BytesIO
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.instructors.models import Instructor


User = get_user_model()

# Never the real project or the real key: a test must not be able to reach the
# owner's live storage, even by accident.
STORAGE_URL = 'https://storage.test'
SERVICE_KEY = 'test-service-role-key'


def png_bytes(size=(64, 64), alpha=0):
    """A PNG with a fully transparent corner pixel, so transparency is testable."""
    image = Image.new('RGBA', size, (200, 30, 30, 255))
    image.putpixel((0, 0), (0, 0, 0, alpha))
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def jpeg_bytes(size=(64, 64)):
    buffer = BytesIO()
    Image.new('RGB', size, (30, 90, 200)).save(buffer, format='JPEG')
    return buffer.getvalue()


def upload(name='photo.png', content=None, content_type='image/png'):
    return SimpleUploadedFile(name, content if content is not None else png_bytes(), content_type=content_type)


@override_settings(SUPABASE_URL=STORAGE_URL, SUPABASE_SERVICE_ROLE_KEY=SERVICE_KEY)
class InstructorPhotoTest(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='גאולים')
        self.instructor = Instructor.objects.create(
            first_name='אלגריה',
            last_name='מדריך',
            phone='0501111111',
            primary_branch=self.branch,
        )

        self.manager = User.objects.create_user(username='mgr@test.com', email='mgr@test.com', password='secret')
        UserProfile.objects.update_or_create(user=self.manager, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        token, _ = Token.objects.get_or_create(user=self.manager)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

        self.worker = User.objects.create_user(username='worker@test.com', email='worker@test.com', password='secret')
        UserProfile.objects.update_or_create(user=self.worker, defaults={'role': UserProfile.ROLE_WORKER})
        self.worker_client = APIClient()
        worker_token, _ = Token.objects.get_or_create(user=self.worker)
        self.worker_client.credentials(HTTP_AUTHORIZATION=f'Token {worker_token.key}')

        self.anonymous = APIClient()
        self.url = f'/api/v1/instructors/{self.instructor.id}/photo/'

    def _post(self, client=None, **kwargs):
        """Upload with the storage call mocked at the requests boundary."""
        with patch('apps.instructors.views.requests.post') as storage_post:
            storage_post.return_value = Mock(status_code=200)
            res = (client or self.client).post(self.url, {'photo': upload(**kwargs)}, format='multipart')
        return res, storage_post

    def test_manager_can_upload_photo(self):
        res, storage_post = self._post()
        self.assertEqual(res.status_code, 200, res.data)

        self.instructor.refresh_from_db()
        self.assertEqual(res.data['photo_url'], self.instructor.photo_url)
        self.assertEqual(storage_post.call_count, 1)

    def test_upload_calls_storage_with_the_right_bucket_headers_and_bytes(self):
        _, storage_post = self._post()
        args, kwargs = storage_post.call_args

        self.assertEqual(
            args[0],
            f'{STORAGE_URL}/storage/v1/object/instructor-photos/{self.instructor.id}.png',
        )
        headers = kwargs['headers']
        self.assertEqual(headers['apikey'], SERVICE_KEY)
        self.assertEqual(headers['Authorization'], f'Bearer {SERVICE_KEY}')
        self.assertEqual(headers['Content-Type'], 'image/png')
        self.assertEqual(headers['x-upsert'], 'true')
        # Long-lived because the saved URL is versioned.
        self.assertEqual(headers['Cache-Control'], 'max-age=31536000')

        # The body is the processed image, not the raw upload.
        sent = Image.open(BytesIO(kwargs['data']))
        self.assertEqual(sent.format, 'PNG')
        self.assertLessEqual(max(sent.size), 512)

    def test_saved_url_is_the_public_read_url_with_a_version(self):
        self._post()
        self.instructor.refresh_from_db()

        self.assertTrue(self.instructor.photo_url.startswith(
            f'{STORAGE_URL}/storage/v1/object/public/instructor-photos/{self.instructor.id}.png?v='
        ))
        # The public path is what a browser reads without any credential.
        self.assertNotIn('/object/instructor-photos/', self.instructor.photo_url)

    def test_replacing_a_photo_changes_the_version(self):
        self._post()
        self.instructor.refresh_from_db()
        first = self.instructor.photo_url

        self._post(content=png_bytes(size=(48, 48), alpha=255))
        self.instructor.refresh_from_db()
        self.assertNotEqual(self.instructor.photo_url, first)

    def test_replacing_a_png_with_a_photograph_removes_the_old_object(self):
        self._post()

        with patch('apps.instructors.views.requests.post') as storage_post, \
                patch('apps.instructors.views.requests.delete') as storage_delete:
            storage_post.return_value = Mock(status_code=200)
            storage_delete.return_value = Mock(status_code=200)
            res = self.client.post(
                self.url,
                {'photo': upload(name='photo.jpg', content=jpeg_bytes(), content_type='image/jpeg')},
                format='multipart',
            )

        self.assertEqual(res.status_code, 200, res.data)
        self.instructor.refresh_from_db()
        self.assertIn(f'{self.instructor.id}.jpg', self.instructor.photo_url)
        self.assertEqual(
            storage_delete.call_args[0][0],
            f'{STORAGE_URL}/storage/v1/object/instructor-photos/{self.instructor.id}.png',
        )

    def test_worker_cannot_upload_photo(self):
        res, storage_post = self._post(client=self.worker_client)
        self.assertEqual(res.status_code, 403, res.data)
        storage_post.assert_not_called()
        self.instructor.refresh_from_db()
        self.assertIsNone(self.instructor.photo_url)

    def test_anonymous_cannot_upload_photo(self):
        res, storage_post = self._post(client=self.anonymous)
        self.assertIn(res.status_code, (401, 403))
        storage_post.assert_not_called()

    def test_worker_cannot_remove_photo(self):
        self._post()
        with patch('apps.instructors.views.requests.delete') as storage_delete:
            res = self.worker_client.delete(self.url)
        self.assertEqual(res.status_code, 403, res.data)
        storage_delete.assert_not_called()
        self.instructor.refresh_from_db()
        self.assertIsNotNone(self.instructor.photo_url)

    def test_anonymous_cannot_remove_photo(self):
        self._post()
        with patch('apps.instructors.views.requests.delete') as storage_delete:
            res = self.anonymous.delete(self.url)
        self.assertIn(res.status_code, (401, 403))
        storage_delete.assert_not_called()

    def test_oversized_upload_is_rejected(self):
        import os
        # Random noise, so the file cannot be compressed back under the cap.
        res, storage_post = self._post(name='big.png', content=os.urandom(3 * 1024 * 1024))
        self.assertEqual(res.status_code, 400)
        self.assertIn('2MB', res.data['error'])
        storage_post.assert_not_called()

    def test_non_image_upload_is_rejected(self):
        res, storage_post = self._post(
            name='cv.pdf', content=b'%PDF-1.4 not an image', content_type='application/pdf'
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['error'], 'הקובץ שנבחר אינו תמונה תקינה')
        storage_post.assert_not_called()

    def test_upload_without_a_file_is_rejected(self):
        with patch('apps.instructors.views.requests.post') as storage_post:
            res = self.client.post(self.url, {}, format='multipart')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['error'], 'לא נבחר קובץ תמונה')
        storage_post.assert_not_called()

    def test_a_rejected_storage_upload_leaves_the_row_alone(self):
        with patch('apps.instructors.views.requests.post') as storage_post:
            storage_post.return_value = Mock(status_code=400)
            res = self.client.post(self.url, {'photo': upload()}, format='multipart')

        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.data['error'], 'שמירת התמונה נכשלה. נסה שוב.')
        self.instructor.refresh_from_db()
        self.assertIsNone(self.instructor.photo_url)

    def test_transparency_survives_the_upload(self):
        _, storage_post = self._post()
        sent = Image.open(BytesIO(storage_post.call_args[1]['data']))
        self.assertEqual(sent.mode, 'RGBA')
        self.assertEqual(sent.getpixel((0, 0))[3], 0)

    def test_a_big_photo_is_downscaled_before_it_is_sent(self):
        _, storage_post = self._post(content=png_bytes(size=(1800, 1200)))
        sent = Image.open(BytesIO(storage_post.call_args[1]['data']))
        self.assertLessEqual(max(sent.size), 512)

    def test_manager_can_remove_photo(self):
        self._post()

        with patch('apps.instructors.views.requests.delete') as storage_delete:
            storage_delete.return_value = Mock(status_code=200)
            res = self.client.delete(self.url)

        self.assertEqual(res.status_code, 204)
        args, kwargs = storage_delete.call_args
        self.assertEqual(
            args[0],
            f'{STORAGE_URL}/storage/v1/object/instructor-photos/{self.instructor.id}.png',
        )
        self.assertEqual(kwargs['headers']['Authorization'], f'Bearer {SERVICE_KEY}')

        self.instructor.refresh_from_db()
        self.assertIsNone(self.instructor.photo_url)

    def test_removing_still_clears_the_row_when_storage_is_unreachable(self):
        import requests as requests_lib
        self._post()

        with patch('apps.instructors.views.requests.delete') as storage_delete:
            storage_delete.side_effect = requests_lib.RequestException('boom')
            res = self.client.delete(self.url)

        self.assertEqual(res.status_code, 204)
        self.instructor.refresh_from_db()
        self.assertIsNone(self.instructor.photo_url)

    def test_photo_url_appears_on_the_instructor_payload(self):
        self._post()
        self.instructor.refresh_from_db()

        res = self.client.get(f'/api/v1/instructors/{self.instructor.id}/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['photo_url'], self.instructor.photo_url)

    def test_the_service_role_key_is_never_returned_to_a_caller(self):
        res, _ = self._post()
        detail = self.client.get(f'/api/v1/instructors/{self.instructor.id}/')
        widget = self.anonymous.get(f'/api/v1/customers/widget/courses/?branch_id={self.branch.id}')

        for payload in (res.content, detail.content, widget.content):
            self.assertNotIn(SERVICE_KEY.encode(), payload)

    def _course_with_lesson(self):
        course = Course.objects.create(
            course_type=CourseType.objects.create(name='קפואירה'),
            name='מתחילים',
            price=300,
            capacity=20,
            branch=self.branch,
        )
        Lesson.objects.create(
            course=course,
            instructor=self.instructor,
            day_of_week=1,
            start_time='17:00',
            end_time='18:00',
        )
        return course

    def test_widget_course_payload_carries_the_public_photo_url(self):
        self._course_with_lesson()
        self._post()
        self.instructor.refresh_from_db()

        res = self.anonymous.get(f'/api/v1/customers/widget/courses/?branch_id={self.branch.id}')
        self.assertEqual(res.status_code, 200)
        lesson_payload = res.data[0]['lessons'][0]
        self.assertEqual(lesson_payload['instructor_name'], self.instructor.full_name)
        self.assertEqual(lesson_payload['instructor_photo_url'], self.instructor.photo_url)
        self.assertIn('/object/public/instructor-photos/', lesson_payload['instructor_photo_url'])

    def test_widget_course_payload_is_null_without_a_photo(self):
        self._course_with_lesson()

        res = self.anonymous.get(f'/api/v1/customers/widget/courses/?branch_id={self.branch.id}')
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data[0]['lessons'][0]['instructor_photo_url'])
