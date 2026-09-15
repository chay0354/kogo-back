"""A lesson change asks for the same discounts a signup asks for.

The quote used to hand the discount service `lesson_id=None`, so the
"additional lesson" price a child already had on their second lesson vanished
the moment that lesson moved to another day. The target lesson is now passed
exactly as `resolve_billing_price` does at signup: never when a per-lesson
tier or bundle price already lowered the figure, and never for a child whose
only lesson is the one being moved.
"""
from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

from apps.courses.models import Lesson
from apps.customers.discount_service import DiscountCalculation
from apps.enrollments.change_pricing import quote_unit_change
from apps.enrollments.models import LessonEnrollment
from apps.enrollments.tests.test_change_lesson_pricing import TODAY, _Base


def _passthrough(**kwargs):
    return DiscountCalculation(
        applicable_discounts=[],
        total_discount_amount=Decimal('0.00'),
        final_price=kwargs['base_price'],
        base_price=kwargs['base_price'],
    )


class AdditionalLessonOnChangeTest(_Base):
    def setUp(self):
        super().setUp()
        # A second Wednesday-course lesson on Sunday to move the child's lesson to.
        self.sun = Lesson.objects.create(
            course=self.course_once, room=self.room, day_of_week=0,
            start_time=time(16, 45), end_time=time(17, 30),
        )

    def _quote(self, target):
        with patch('apps.customers.discount_service.DiscountService.evaluate_discounts_for_payment',
                   side_effect=_passthrough) as evaluate:
            quote_unit_change(enrollment=self.enrollment, target_lessons=[target], target_bundle=None, today=TODAY)
        evaluate.assert_called_once()
        return evaluate.call_args.kwargs

    def test_the_only_lesson_moving_is_not_an_additional_lesson(self):
        # The Wednesday row still sits on the roster while we quote; it must
        # not make its own replacement look like a second lesson.
        self.assertIsNone(self._quote(self.sun)['lesson_id'])

    def test_a_second_lesson_keeps_its_additional_lesson_price_when_it_moves(self):
        LessonEnrollment.objects.create(lesson=self.mon, child=self.child, status='active', start_date=date(2026, 8, 1))
        args = self._quote(self.sun)
        self.assertEqual(args['lesson_id'], str(self.sun.id))
        self.assertEqual(args['base_price'], Decimal('260.00'))

    def test_a_lesson_on_a_payment_problem_still_counts_as_the_other_lesson(self):
        LessonEnrollment.objects.create(lesson=self.mon, child=self.child, status='payments_problem', start_date=date(2026, 8, 1))
        self.assertEqual(self._quote(self.sun)['lesson_id'], str(self.sun.id))

    def test_a_trial_booking_is_not_the_other_lesson(self):
        LessonEnrollment.objects.create(
            lesson=self.mon, child=self.child, status='active', start_date=date(2026, 8, 1),
            trial_lesson_date=date(2026, 9, 14),
        )
        self.assertIsNone(self._quote(self.sun)['lesson_id'])

    def test_a_tier_price_is_never_discounted_a_second_time(self):
        LessonEnrollment.objects.create(lesson=self.mon, child=self.child, status='active', start_date=date(2026, 8, 1))
        self.sun.additional_course_prices = [{'course_index': 2, 'price': 200}]
        self.sun.save()
        args = self._quote(self.sun)
        self.assertEqual(args['base_price'], Decimal('200.00'))
        self.assertIsNone(args['lesson_id'])

    def test_a_bundle_target_is_priced_by_the_bundle_alone(self):
        LessonEnrollment.objects.create(lesson=self.sun, child=self.child, status='active', start_date=date(2026, 8, 1))
        with patch('apps.customers.discount_service.DiscountService.evaluate_discounts_for_payment',
                   side_effect=_passthrough) as evaluate:
            quote_unit_change(enrollment=self.enrollment, target_lessons=[self.mon, self.thu], target_bundle=self.bundle, today=TODAY)
        self.assertIsNone(evaluate.call_args.kwargs['lesson_id'])
        self.assertEqual(evaluate.call_args.kwargs['base_price'], Decimal('335.00'))
