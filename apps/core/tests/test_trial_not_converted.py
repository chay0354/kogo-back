"""Children who came to a trial and never signed up for a course."""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.enrollments.models import LessonEnrollment

User = get_user_model()
URL = '/api/v1/core/dashboard/trial-not-converted/'
TRIAL_DAY = date.today() - timedelta(days=10)


def _trial(child, lesson, outcome='attended', trial_date=TRIAL_DAY, number=1):
    return LessonEnrollment.objects.create(
        child=child, lesson=lesson, status='active',
        trial_lesson_date=trial_date, trial_outcome=outcome, trial_number=number,
    )


def _paying(child, lesson):
    return LessonEnrollment.objects.create(child=child, lesson=lesson, status='active')


class TrialNotConvertedTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        user = User.objects.create_user(
            username='mgr-trial@x.com', email='mgr-trial@x.com',
            password='pass12345!', is_active=True,
        )
        UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
        self.lesson = TestDataFactory.create_lesson()

    def _names(self, **params):
        res = self.client.get(URL, params)
        self.assertEqual(res.status_code, 200)
        return [r['child_name'] for r in res.data['results']]

    def _child(self, first, status='trial_completed'):
        family = TestDataFactory.create_family()
        TestDataFactory.create_parent(family=family)
        return TestDataFactory.create_child(family=family, first_name=first, status=status)

    def test_a_child_who_attended_and_never_signed_up_is_listed(self):
        child = self._child('הגיע')
        _trial(child, self.lesson)
        names = self._names()
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith('הגיע'))

    def test_a_no_show_is_listed_and_marked_as_one(self):
        """Two different calls to make, so both are listed and each says which."""
        _trial(self._child('לא הגיע'), self.lesson, outcome='no_show')
        rows = self.client.get(URL).data['results']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['outcome'], 'no_show')
        self.assertEqual(rows[0]['outcome_label'], 'לא הגיע')

    def test_the_two_outcomes_are_counted_apart(self):
        _trial(self._child('הגיע'), self.lesson)
        _trial(self._child('לא הגיע'), TestDataFactory.create_lesson(), outcome='no_show')
        data = self.client.get(URL).data
        self.assertEqual(data['count'], 2)
        self.assertEqual(data['attended'], 1)
        self.assertEqual(data['no_show'], 1)

    def test_one_outcome_can_be_asked_for_alone(self):
        _trial(self._child('הגיע'), self.lesson)
        _trial(self._child('לא הגיע'), TestDataFactory.create_lesson(), outcome='no_show')
        rows = self.client.get(URL, {'outcome': 'no_show'}).data['results']
        self.assertEqual([r['outcome'] for r in rows], ['no_show'])

    def test_an_unmarked_register_is_not_listed(self):
        """A register nobody filled in is not a fact about the child."""
        _trial(self._child('לא סומן'), self.lesson, outcome='unmarked')
        self.assertEqual(self._names(), [])

    def test_a_child_who_signed_up_afterwards_drops_off(self):
        child = self._child('התקדם', status='active')
        _trial(child, self.lesson)
        _paying(child, TestDataFactory.create_lesson())
        self.assertEqual(self._names(), [])

    def test_two_trials_for_one_child_are_one_row(self):
        child = self._child('פעמיים')
        # A second trial is a second enrollment: one row per (lesson, child).
        _trial(child, TestDataFactory.create_lesson(),
               trial_date=TRIAL_DAY - timedelta(days=30), number=1)
        _trial(child, self.lesson, trial_date=TRIAL_DAY, number=2)
        rows = self.client.get(URL).data['results']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['trial_number'], 2, 'הניסיון האחרון הוא זה שמוצג')

    def test_the_row_carries_what_it_takes_to_call_them(self):
        child = self._child('לחייג')
        _trial(child, self.lesson)
        row = self.client.get(URL).data['results'][0]
        self.assertTrue(row['parent_phone'])
        self.assertTrue(row['parent_name'])
        self.assertTrue(row['course_name'])
        self.assertEqual(row['trial_date'], TRIAL_DAY.isoformat())
        self.assertEqual(row['days_since_trial'], 10)

    def test_filtered_by_branch(self):
        _trial(self._child('כאן'), self.lesson)
        other = TestDataFactory.create_lesson()
        _trial(self._child('שם'), other)
        branch_id = str(self.lesson.course.branch_id)
        rows = self.client.get(URL, {'branch_id': branch_id}).data['results']
        self.assertTrue(all(r['branch_id'] == branch_id for r in rows))

    def test_filtered_by_course(self):
        _trial(self._child('בחוג'), self.lesson)
        _trial(self._child('בחוג אחר'), TestDataFactory.create_lesson())
        rows = self.client.get(URL, {'course_id': str(self.lesson.course_id)}).data['results']
        self.assertEqual(len(rows), 1)

    def test_nobody_attended_is_an_empty_list_not_an_error(self):
        res = self.client.get(URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['count'], 0)

    def test_a_worker_is_refused(self):
        worker = User.objects.create_user(username='w-trial@x.com', email='w-trial@x.com',
                                          password='pass12345!', is_active=True)
        UserProfile.objects.update_or_create(user=worker, defaults={'role': UserProfile.ROLE_WORKER})
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=worker).key}')
        self.assertEqual(c.get(URL).status_code, 403)
