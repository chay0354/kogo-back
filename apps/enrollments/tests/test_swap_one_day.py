"""One day of a twice-a-week track swapped for another group's lesson (owner, 15.10.2026).

A family is on a track (Monday + Thursday) at one price. The Thursday was
cancelled; they go to another group's Wednesday at the same branch instead,
with another instructor, for the same money and without signing up again.
"""
from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.core.payment_service import enroll_child_in_paid_lessons, heal_missing_bundle_enrollments
from apps.courses.models import Course, CourseType, Lesson, LessonBundle
from apps.customers.models import Child, Family, Parent, Payment, RecurringPayment
from apps.customers.problem_flags import problems_for_children
from apps.customers.serializers import _serialize_lesson_enrollment
from apps.enrollments.models import LessonEnrollment
from apps.instructors.models import Instructor


class _Base(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='מרכז העיר')
        self.room = Room.objects.create(branch=self.branch, name='Studio', capacity=20)
        ctype = CourseType.objects.create(name='ג׳אז')
        self.dana = Instructor.objects.create(first_name='דנה', last_name='לוי', phone='0500000001')
        self.yael = Instructor.objects.create(first_name='יעל', last_name='כהן', phone='0500000002')
        self.track_course = Course.objects.create(course_type=ctype, name='פאנקי ג׳אז', price=335, capacity=10, branch=self.branch)
        self.other_course = Course.objects.create(course_type=ctype, name='היפ הופ', price=235, capacity=10, branch=self.branch)
        self.mon = Lesson.objects.create(course=self.track_course, room=self.room, instructor=self.dana, day_of_week=1, start_time=time(16, 0), end_time=time(17, 0))
        self.thu = Lesson.objects.create(course=self.track_course, room=self.room, instructor=self.dana, day_of_week=4, start_time=time(16, 0), end_time=time(17, 0))
        self.wed_other = Lesson.objects.create(course=self.other_course, room=self.room, instructor=self.yael, day_of_week=3, start_time=time(17, 0), end_time=time(18, 0))
        self.bundle = LessonBundle.objects.create(course=self.track_course, combined_price=Decimal('335.00'))
        self.bundle.lessons.set([self.mon, self.thu])

        self.family = Family.objects.create(name='Cohen', phone='0501234567', branch=self.branch)
        Parent.objects.create(family=self.family, first_name='Avi', last_name='Cohen', phone='0501234567', is_primary=True)
        self.child = Child.objects.create(family=self.family, first_name='Noa', last_name='Cohen', birth_date=date(2018, 1, 1), gender='female', status='active')
        self.mon_row = LessonEnrollment.objects.create(lesson=self.mon, child=self.child, status='active', bundle=self.bundle, start_date=date(2026, 9, 1))
        self.thu_row = LessonEnrollment.objects.create(lesson=self.thu, child=self.child, status='active', bundle=self.bundle, start_date=date(2026, 9, 1))
        self.payment = Payment.objects.create(
            child=self.child, family=self.family, branch=self.branch, lesson=self.mon, bundle=self.bundle,
            payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('335.00'), discount_amount=Decimal('10.00'), final_amount=Decimal('325.00'),
        )
        self.recurring = RecurringPayment.objects.create(
            child=self.child, initial_payment=self.payment, status='active',
            tranzila_token='tok_saved', card_expire_month=12, card_expire_year=2030,
            base_amount=Decimal('335.00'), amount=Decimal('325.00'), billing_day=1,
            start_date=date(2026, 9, 1), next_billing_date=date(2026, 11, 1),
            discount_details=[{'type': 'second_child', 'name': 'הנחת אחים', 'amount_deducted': '10.00', 'reason': 'הנחת אחים'}],
        )
        User = get_user_model()
        self.manager = User.objects.create_user(username='m@test.com', email='m@test.com', password='x', first_name='מנהלת', last_name='משרד')
        UserProfile.objects.update_or_create(user=self.manager, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=self.manager).key}')

    def url(self, row, quote=False):
        return f'/api/v1/enrollments/lesson-enrollments/{row.id}/change-lesson/{"quote/" if quote else ""}'

    def swap(self, row=None, lesson=None, **extra):
        body = {'swap_day': True, 'lesson_id': str((lesson or self.wed_other).id)}
        body.update(extra)
        with patch('apps.core.tranzila_service.TranzilaService.charge_with_token') as charge:
            res = self.client.post(self.url(row or self.thu_row), body, format='json')
            charge.assert_not_called()
        return res

    def active_lessons(self):
        return set(
            LessonEnrollment.objects.filter(child=self.child, status='active').values_list('lesson_id', flat=True)
        )


class SwapTest(_Base):
    def test_quote_says_the_price_does_not_change(self):
        res = self.client.post(self.url(self.thu_row, quote=True), {'swap_day': True, 'lesson_id': str(self.wed_other.id)}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['direction'], 'same')
        self.assertEqual(res.data['current_amount'], '325.00')
        self.assertEqual(res.data['new_amount'], '325.00')
        self.assertIn('פעמיים בשבוע', res.data['target_label'])

    def test_the_cancelled_day_is_replaced_and_the_other_day_stays(self):
        res = self.swap(reason='יום חמישי בוטל')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.active_lessons(), {self.mon.id, self.wed_other.id})
        stand_in = LessonEnrollment.objects.get(child=self.child, lesson=self.wed_other)
        self.assertEqual(stand_in.bundle_id, self.bundle.id)          # still the same registration
        self.mon_row.refresh_from_db()
        self.assertEqual(self.mon_row.bundle_id, self.bundle.id)
        self.assertEqual({row['lesson_id'] for row in res.data['enrollments']}, {str(self.mon.id), str(self.wed_other.id)})
        self.assertIn('יום מוחלף', stand_in.notes)
        self.assertIn('חמישי 16:00', stand_in.notes)
        self.assertIn('רביעי 17:00', stand_in.notes)
        self.assertIn('יעל כהן', stand_in.notes)
        self.assertIn('סיבה: יום חמישי בוטל', stand_in.notes)
        self.assertIn('מנהלת משרד', stand_in.notes)

    def test_the_standing_order_and_its_discount_are_untouched(self):
        self.swap()
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.amount, Decimal('325.00'))
        self.assertIsNone(self.recurring.pending_amount)
        self.assertEqual(self.recurring.status, 'active')
        self.assertFalse(Payment.objects.filter(child=self.child, payment_type='one_time').exists())

    def test_the_payment_stays_on_the_track_lesson(self):
        # The day swapped is the one the payment points at: it moves to the day
        # that stays on the track, never to the other group's lesson.
        res = self.swap(row=self.mon_row)
        self.assertEqual(res.status_code, 200, res.content)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.lesson_id, self.thu.id)
        self.assertEqual(self.payment.bundle_id, self.bundle.id)

    def test_only_the_same_branch(self):
        far = Branch.objects.create(name='סניף אחר')
        far_room = Room.objects.create(branch=far, name='S', capacity=20)
        far_course = Course.objects.create(course_type=self.other_course.course_type, name='היפ הופ', price=235, capacity=10, branch=far)
        far_lesson = Lesson.objects.create(course=far_course, room=far_room, day_of_week=2, start_time=time(17, 0), end_time=time(18, 0))
        res = self.swap(lesson=far_lesson)
        self.assertEqual(res.status_code, 400)
        self.assertIn('אותו סניף', res.data['error'])
        self.assertEqual(self.active_lessons(), {self.mon.id, self.thu.id})

    def test_only_a_track(self):
        self.thu_row.delete()
        self.mon_row.bundle = None
        self.mon_row.save()
        res = self.swap(row=self.mon_row)
        self.assertEqual(res.status_code, 400)
        self.assertIn('מסלול', res.data['error'])

    def test_not_a_trial(self):
        trial = LessonEnrollment.objects.create(lesson=self.wed_other, child=self.child, status='active', trial_lesson_date=date(2026, 10, 21))
        res = self.client.post(self.url(trial), {'swap_day': True, 'lesson_id': str(self.thu.id)}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_a_lesson_the_child_already_pays_for_apart_is_refused(self):
        LessonEnrollment.objects.create(lesson=self.wed_other, child=self.child, status='active')
        res = self.swap()
        self.assertEqual(res.status_code, 400)
        self.assertIn('כבר רשום', res.data['error'])

    def test_a_trial_still_ahead_on_that_lesson_is_refused(self):
        LessonEnrollment.objects.create(lesson=self.wed_other, child=self.child, status='active', trial_lesson_date=date(2099, 1, 7))
        res = self.swap()
        self.assertEqual(res.status_code, 400)
        self.assertIn('ניסיון', res.data['error'])

    def test_an_old_trial_on_that_lesson_becomes_the_regular_place(self):
        old = LessonEnrollment.objects.create(
            lesson=self.wed_other, child=self.child, status='inactive',
            trial_lesson_date=date(2026, 8, 19), trial_outcome='attended', end_date=date(2026, 8, 19),
        )
        res = self.swap()
        self.assertEqual(res.status_code, 200, res.content)
        old.refresh_from_db()
        self.assertEqual(old.status, 'active')
        self.assertIsNone(old.trial_lesson_date)
        self.assertEqual(old.trial_outcome, 'attended')
        self.assertEqual(old.bundle_id, self.bundle.id)
        self.thu_row.refresh_from_db()
        self.assertEqual(self.thu_row.status, 'inactive')

    def test_a_full_lesson_is_refused(self):
        self.wed_other.capacity = 1
        self.wed_other.save()
        other_child = Child.objects.create(family=self.family, first_name='Dan', last_name='Cohen', birth_date=date(2017, 1, 1), gender='male', status='active')
        LessonEnrollment.objects.create(lesson=self.wed_other, child=other_child, status='active')
        res = self.swap()
        self.assertEqual(res.status_code, 400)
        self.assertIn('מלא', res.data['error'])
        self.assertEqual(self.active_lessons(), {self.mon.id, self.thu.id})


class AfterTheSwapTest(_Base):
    def setUp(self):
        super().setUp()
        res = self.swap()
        assert res.status_code == 200, res.content
        self.stand_in = LessonEnrollment.objects.get(child=self.child, lesson=self.wed_other)

    def test_a_new_card_on_the_track_does_not_put_the_child_back_on_the_cancelled_day(self):
        enroll_child_in_paid_lessons(child=self.child, lesson=self.mon, bundle=self.bundle)
        self.assertEqual(self.active_lessons(), {self.mon.id, self.wed_other.id})

    def test_the_split_track_healer_does_not_either(self):
        result = heal_missing_bundle_enrollments()
        self.assertEqual(result['enrollments_created'], 0)
        self.assertEqual(self.active_lessons(), {self.mon.id, self.wed_other.id})

    def test_without_a_swap_the_healer_still_adds_a_missing_day(self):
        self.stand_in.delete()
        heal_missing_bundle_enrollments()
        self.assertEqual(self.active_lessons(), {self.mon.id, self.thu.id})

    def test_the_customers_page_reads_one_registration(self):
        row = _serialize_lesson_enrollment(self.stand_in)
        self.assertEqual(row['course_id'], str(self.other_course.id))
        self.assertEqual(row['track_course_id'], str(self.track_course.id))
        self.assertEqual(row['track_course_name'], 'פאנקי ג׳אז')
        self.assertTrue(row['swapped_day'])
        kept = _serialize_lesson_enrollment(self.mon_row)
        self.assertEqual(kept['track_course_id'], str(self.track_course.id))
        self.assertFalse(kept['swapped_day'])

    def test_the_customers_list_carries_the_track(self):
        res = self.client.get('/api/v1/customers/children/', {'family': str(self.family.id)})
        self.assertEqual(res.status_code, 200, res.content)
        results = res.data['results'] if isinstance(res.data, dict) else res.data
        child = next(row for row in results if row['id'] == str(self.child.id))
        rows = {row['lesson_id']: row for row in child['enrollments']}
        self.assertEqual(rows[str(self.wed_other.id)]['track_course_id'], str(self.track_course.id))
        self.assertTrue(rows[str(self.wed_other.id)]['swapped_day'])

    def test_no_problem_mark_for_the_replacement_day(self):
        problems = problems_for_children([self.child])[self.child.id]
        self.assertEqual([problem.code for problem in problems], [])

    def test_the_replacement_row_can_still_be_edited(self):
        res = self.client.patch(f'/api/v1/enrollments/lesson-enrollments/{self.stand_in.id}/', {'notes': 'הערה'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)

    def test_moving_back_to_the_track_restores_the_day_at_the_same_price(self):
        res = self.client.post(self.url(self.mon_row), {'bundle_id': str(self.bundle.id)}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.active_lessons(), {self.mon.id, self.thu.id})
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.amount, Decimal('325.00'))
        self.assertIsNone(self.recurring.pending_amount)

    def test_a_second_swap_of_the_replacement_day(self):
        fri = Lesson.objects.create(course=self.other_course, room=self.room, instructor=self.yael, day_of_week=5, start_time=time(10, 0), end_time=time(11, 0))
        res = self.swap(row=self.stand_in, lesson=fri)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.active_lessons(), {self.mon.id, fri.id})
        row = LessonEnrollment.objects.get(child=self.child, lesson=fri)
        self.assertEqual(row.bundle_id, self.bundle.id)

    def test_removing_the_class_removes_both_days_and_stops_the_standing_order(self):
        with patch('apps.core.payment_service.PaymentService.cancel_subscription', return_value={'success': True}) as cancel:
            res = self.client.post(f'/api/v1/enrollments/lesson-enrollments/{self.stand_in.id}/drop-course/', {}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.active_lessons(), set())
        self.assertEqual(cancel.call_args.kwargs['recurring_payment_id'], str(self.recurring.id))
