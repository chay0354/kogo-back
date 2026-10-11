"""A lesson that moves is priced by its place among the child's classes.

The "another class" discount belongs to the lesson's place, not to the family:
a child's second class keeps the second-class price when it moves, an only
class never becomes a second one by moving (the row being moved still sits on
the roster while we quote), and a price a group set for itself as a second
class is not lowered a second time.
"""
from datetime import date, time
from decimal import Decimal

from apps.courses.models import Course, Lesson
from apps.customers.financial_models import Discount
from apps.enrollments.change_pricing import quote_unit_change
from apps.enrollments.models import LessonEnrollment
from apps.enrollments.tests.test_change_lesson_pricing import TODAY, _Base


class AdditionalLessonOnChangeTest(_Base):
    def setUp(self):
        super().setUp()
        Discount.objects.create(
            name='הנחת שיעור נוסף לילד פעיל', discount_type='fixed', value=Decimal('10.00'),
            applies_to='child', promotion_type='permanent', is_built_in=True, is_active=True,
        )
        # A dearer class to move the Wednesday lesson to.
        dearer = Course.objects.create(
            course_type=self.course_once.course_type, name='אקרובטיקה', price=300, capacity=10, branch=self.branch,
        )
        self.sun = Lesson.objects.create(
            course=dearer, room=self.room, day_of_week=0, start_time=time(16, 45), end_time=time(17, 30),
        )

    def _quote(self, *targets, bundle=None):
        return quote_unit_change(
            enrollment=self.enrollment, target_lessons=list(targets), target_bundle=bundle, today=TODAY,
        )

    def test_the_only_lesson_moving_is_not_an_additional_lesson(self):
        # The Wednesday row still sits on the roster while we quote; it must
        # not make its own replacement look like a second lesson.
        quote = self._quote(self.sun)
        self.assertEqual(quote['new_amount'], '300.00')
        self.assertEqual(quote['discounts'], [])

    def test_a_second_lesson_keeps_its_additional_lesson_price_when_it_moves(self):
        LessonEnrollment.objects.create(lesson=self.mon, child=self.child, status='active', start_date=date(2026, 8, 1))
        quote = self._quote(self.sun)
        self.assertEqual(quote['new_base_price'], '300.00')
        self.assertEqual(quote['new_amount'], '290.00')
        self.assertEqual([line['value'] for line in quote['discounts']], ['10.00'])

    def test_a_lesson_on_a_payment_problem_still_counts_as_the_other_lesson(self):
        LessonEnrollment.objects.create(lesson=self.mon, child=self.child, status='payments_problem', start_date=date(2026, 8, 1))
        self.assertEqual(self._quote(self.sun)['new_amount'], '290.00')

    def test_a_trial_booking_is_not_the_other_lesson(self):
        LessonEnrollment.objects.create(
            lesson=self.mon, child=self.child, status='active', start_date=date(2026, 8, 1),
            trial_lesson_date=date(2026, 9, 14),
        )
        self.assertEqual(self._quote(self.sun)['new_amount'], '300.00')

    def test_a_tier_price_is_never_discounted_a_second_time(self):
        LessonEnrollment.objects.create(lesson=self.mon, child=self.child, status='active', start_date=date(2026, 8, 1))
        self.sun.additional_course_prices = [{'course_index': 2, 'price': 200}]
        self.sun.save()
        quote = self._quote(self.sun)
        self.assertEqual(quote['new_base_price'], '200.00')
        self.assertEqual(quote['new_amount'], '200.00')
        self.assertEqual(quote['discounts'], [])

    def test_a_bundle_target_is_priced_by_the_bundle_alone(self):
        LessonEnrollment.objects.create(lesson=self.sun, child=self.child, status='active', start_date=date(2026, 8, 1))
        quote = self._quote(self.mon, self.thu, bundle=self.bundle)
        self.assertEqual(quote['new_amount'], '335.00')
        self.assertEqual(quote['discounts'], [])

    def test_a_fixed_price_for_another_class_is_the_whole_price(self):
        Discount.objects.filter(is_built_in=True).update(discount_type='fixed_final_price', value=Decimal('180.00'))
        LessonEnrollment.objects.create(lesson=self.mon, child=self.child, status='active', start_date=date(2026, 8, 1))
        # The class the child sits on costs less than the fixed price, so the
        # fixed price does not touch it; the dearer one costs the fixed price.
        self.course_once.price = 150
        self.course_once.save()
        # A sibling discount from signup does not come off a fixed price — as at signup.
        self.recurring.discount_details = [{
            'name': 'הנחת ילד שני', 'type': 'second_child', 'value': '10.00', 'amount_deducted': '10.00', 'reason': '',
        }]
        self.recurring.amount = Decimal('140.00')
        self.recurring.save()
        quote = self._quote(self.sun)
        self.assertEqual(quote['new_amount'], '180.00')
        self.assertEqual(quote['direction'], 'up')

    def test_under_a_fixed_price_every_other_class_costs_the_same(self):
        Discount.objects.filter(is_built_in=True).update(discount_type='fixed_final_price', value=Decimal('180.00'))
        LessonEnrollment.objects.create(lesson=self.mon, child=self.child, status='active', start_date=date(2026, 8, 1))
        self.recurring.amount = Decimal('180.00')
        self.recurring.save()
        quote = self._quote(self.sun)
        self.assertEqual(quote['direction'], 'same')
        self.assertEqual(quote['new_amount'], '180.00')
