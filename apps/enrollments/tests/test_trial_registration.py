"""
The trial button has two masters: a studio-wide rule, and each lesson's own
answer. These hold the two apart — the rule moves every lesson that follows
it, a lesson set by hand keeps its answer through the flip — and hold the
settings listing to the same scoping as the rest of the CRM.
"""
from datetime import time

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.enrollments.models import TrialRegistrationPolicy
from apps.enrollments.trial_policy import trial_registration_open_for

User = get_user_model()
POLICY_URL = '/api/v1/enrollments/trial-registration-policy/'
LESSONS_URL = '/api/v1/enrollments/trial-registration/lessons/'


def make_user(username, role):
    user = User.objects.create_user(username=username, password='pw-for-tests')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return User.objects.get(pk=user.pk)


class TrialRegistrationRuleTests(APITestCase):
    def setUp(self):
        city = City.objects.create(name='עיר')
        self.north = Branch.objects.create(name='סניף צפון', city=city)
        self.south = Branch.objects.create(name='סניף דרום', city=city)
        self.dance = CourseType.objects.create(name='היפהופ')
        self.judo = CourseType.objects.create(name='ג\'ודו')
        self.kids = Course.objects.create(
            name='היפהופ ילדים', branch=self.north, course_type=self.dance,
            price=320, capacity=20, min_age=6, max_age=9,
        )
        self.teens = Course.objects.create(
            name='ג\'ודו נוער', branch=self.south, course_type=self.judo,
            price=380, capacity=20, min_age=12, max_age=None,
        )
        self.kids_lesson = Lesson.objects.create(
            course=self.kids, day_of_week=0, start_time=time(17, 0), end_time=time(18, 0),
        )
        self.teens_lesson = Lesson.objects.create(
            course=self.teens, day_of_week=2, start_time=time(19, 0), end_time=time(20, 0),
        )
        self.manager = make_user('manager-trial@test', UserProfile.ROLE_MANAGER)
        self.partner = make_user('partner-trial@test', UserProfile.ROLE_PARTNER)
        self.partner.profile.assigned_branches.add(self.north)
        self.partner = User.objects.get(pk=self.partner.pk)

    # --- the rule and a lesson's own answer -------------------------------

    def test_trials_are_open_until_someone_says_otherwise(self):
        self.assertTrue(trial_registration_open_for(self.kids_lesson))

    def test_closing_the_rule_closes_every_lesson_that_follows_it(self):
        policy = TrialRegistrationPolicy.current()
        policy.trials_open = False
        policy.save()
        self.assertFalse(trial_registration_open_for(self.kids_lesson))
        self.assertFalse(trial_registration_open_for(self.teens_lesson))

    def test_a_lesson_set_by_hand_keeps_its_answer_through_the_flip(self):
        self.kids_lesson.trial_registration_open = True
        self.kids_lesson.save()
        self.teens_lesson.trial_registration_open = False
        self.teens_lesson.save()
        policy = TrialRegistrationPolicy.current()
        policy.trials_open = False
        policy.save()
        self.assertTrue(trial_registration_open_for(self.kids_lesson))
        policy.trials_open = True
        policy.save()
        self.assertFalse(trial_registration_open_for(self.teens_lesson))

    # --- the API -----------------------------------------------------------

    def test_manager_flips_the_rule_and_partner_may_only_read_it(self):
        self.client.force_authenticate(self.manager)
        res = self.client.put(POLICY_URL, {'trials_open': False}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(res.json()['trials_open'])

        self.client.force_authenticate(self.partner)
        self.assertEqual(self.client.get(POLICY_URL).json()['trials_open'], False)
        res = self.client.put(POLICY_URL, {'trials_open': True}, format='json')
        self.assertEqual(res.status_code, 403)
        self.assertFalse(TrialRegistrationPolicy.current().trials_open)

    def test_the_rule_only_takes_a_boolean(self):
        self.client.force_authenticate(self.manager)
        res = self.client.put(POLICY_URL, {'trials_open': 'no'}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_listing_narrows_by_branch_type_and_age_and_says_what_each_comes_to(self):
        self.teens_lesson.trial_registration_open = False
        self.teens_lesson.save()
        self.client.force_authenticate(self.manager)

        everything = self.client.get(LESSONS_URL).json()
        self.assertTrue(everything['trials_open_by_default'])
        self.assertEqual({r['course_name'] for r in everything['results']}, {'היפהופ ילדים', 'ג\'ודו נוער'})

        by_branch = self.client.get(LESSONS_URL, {'branch': str(self.south.id)}).json()['results']
        self.assertEqual([r['course_name'] for r in by_branch], ['ג\'ודו נוער'])
        self.assertIs(by_branch[0]['override'], False)
        self.assertIs(by_branch[0]['effective'], False)

        by_type = self.client.get(LESSONS_URL, {'course_type': str(self.dance.id)}).json()['results']
        self.assertEqual([r['course_name'] for r in by_type], ['היפהופ ילדים'])
        self.assertIsNone(by_type[0]['override'])
        self.assertIs(by_type[0]['effective'], True)

        # 14 fits the open-ended teen range and misses the 6–9 one.
        by_age = self.client.get(LESSONS_URL, {'age': '14'}).json()['results']
        self.assertEqual([r['course_name'] for r in by_age], ['ג\'ודו נוער'])
        self.assertEqual(self.client.get(LESSONS_URL, {'age': 'x'}).status_code, 400)

    def test_partner_lists_only_their_branches(self):
        self.client.force_authenticate(self.partner)
        rows = self.client.get(LESSONS_URL).json()['results']
        self.assertEqual({r['branch_name'] for r in rows}, {'סניף צפון'})
