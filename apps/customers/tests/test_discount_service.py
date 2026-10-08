"""
Unit tests for DiscountService.

Tests coverage:
- Early Sign-Up Discount: date range validation, percentage/fixed value
- Second Child Discount: automatically applied to 2nd+ children
- Multiple discounts: additive combination
- No discounts: returns base price
- Fixed final price discounts
"""
from decimal import Decimal
from datetime import date, timedelta
from django.test import TestCase

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.discount_service import DiscountService
from apps.customers.financial_models import Discount
from apps.enrollments.models import LessonEnrollment


def enroll_child_to_team(child, lesson=None, trial=False):
    """Create a lesson enrollment; trial=True marks it as a trial-lesson enrollment."""
    if lesson is None:
        lesson = TestDataFactory.create_lesson()
    return LessonEnrollment.objects.create(
        lesson=lesson,
        child=child,
        status='active',
        trial_lesson_date=date.today() if trial else None,
    )


class DiscountServiceEarlySignupTest(TestCase):
    """Test DiscountService early signup discount logic"""
    
    def setUp(self):
        self.service = DiscountService()
        self.family = TestDataFactory.create_family()
        self.child = TestDataFactory.create_child(family=self.family)
        self.branch = self.family.branch
    
    def test_early_signup_fixed_discount(self):
        """Test early signup with fixed amount discount"""
        # Create early signup discount (fixed 75 NIS off)
        # Must be is_built_in=True and name contains "רישום מוקדם"
        discount = Discount.objects.create(
            name="הנחת רישום מוקדם",
            discount_type='fixed',
            value=Decimal('75.00'),
            applies_to='child',
            promotion_type='temporary',
            start_date=date.today() - timedelta(days=5),
            end_date=date.today() + timedelta(days=25),
            is_active=True,
            is_built_in=True
        )
        
        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(self.child.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )
        
        self.assertEqual(result.base_price, Decimal('350.00'))
        self.assertEqual(result.total_discount_amount, Decimal('75.00'))
        self.assertEqual(result.final_price, Decimal('275.00'))
        self.assertEqual(len(result.applicable_discounts), 1)
        self.assertEqual(result.applicable_discounts[0].name, "הנחת רישום מוקדם")
    
    def test_early_signup_fixed_final_price(self):
        """Test early signup with fixed final price"""
        # Create early signup discount (fixed final price 299 NIS)
        # Must be is_built_in=True and name contains "רישום מוקדם"
        discount = Discount.objects.create(
            name="מבצע רישום מוקדם",
            discount_type='fixed_final_price',
            value=Decimal('299.00'),
            applies_to='child',
            promotion_type='temporary',
            start_date=date.today() - timedelta(days=5),
            end_date=date.today() + timedelta(days=25),
            is_active=True,
            is_built_in=True
        )
        
        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(self.child.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )
        
        self.assertEqual(result.final_price, Decimal('299.00'))
        self.assertEqual(result.total_discount_amount, Decimal('51.00'))
    
    def test_early_signup_outside_date_range(self):
        """Test early signup discount not applied outside date range"""
        # Create discount for past dates with built-in flag and identifier
        discount = Discount.objects.create(
            name="הנחת רישום מוקדם שפגה",
            discount_type='fixed',
            value=Decimal('75.00'),
            applies_to='child',
            promotion_type='temporary',
            start_date=date.today() - timedelta(days=60),
            end_date=date.today() - timedelta(days=30),
            is_active=True,
            is_built_in=True
        )
        
        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(self.child.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )
        
        # No discount should be applied
        self.assertEqual(result.final_price, Decimal('350.00'))
        self.assertEqual(result.total_discount_amount, Decimal('0.00'))
        self.assertEqual(len(result.applicable_discounts), 0)
    
    def test_early_signup_inactive_discount(self):
        """Test inactive early signup discount is not applied"""
        # Create inactive discount with identifier
        discount = Discount.objects.create(
            name="הנחת רישום מוקדם לא פעילה",
            discount_type='fixed',
            value=Decimal('75.00'),
            applies_to='child',
            promotion_type='temporary',
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30),
            is_active=False,
            is_built_in=True
        )
        
        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(self.child.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )
        
        self.assertEqual(result.final_price, Decimal('350.00'))
        self.assertEqual(len(result.applicable_discounts), 0)


class DiscountServiceSecondChildTest(TestCase):
    """Test DiscountService second child discount logic"""
    
    def setUp(self):
        self.service = DiscountService()
        self.family = TestDataFactory.create_family()
        self.branch = self.family.branch
    
    def test_second_child_discount_applied(self):
        """Discount applies to a child only when a sibling is already on a team"""
        # Create second child discount - must be is_built_in=True and name contains "ילד שני"
        discount = Discount.objects.create(
            name="הנחת ילד שני",
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='permanent',
            is_active=True,
            is_built_in=True
        )
        
        # Create two children; first child is signed to a team
        child1 = TestDataFactory.create_child(family=self.family, first_name="ראשון")
        child2 = TestDataFactory.create_child(family=self.family, first_name="שני")
        enroll_child_to_team(child1)
        
        # First child - no discount (no OTHER sibling on a team)
        result1 = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(child1.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )
        
        self.assertEqual(result1.final_price, Decimal('350.00'))
        self.assertEqual(len(result1.applicable_discounts), 0)
        
        # Second child - discount applied (sibling already on a team)
        result2 = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(child2.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )
        
        self.assertEqual(result2.final_price, Decimal('300.00'))
        self.assertEqual(result2.total_discount_amount, Decimal('50.00'))
        self.assertEqual(len(result2.applicable_discounts), 1)
    
    def test_third_child_gets_discount(self):
        """Test third child also gets second child discount"""
        discount = Discount.objects.create(
            name="הנחת ילד שני",
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='permanent',
            is_active=True,
            is_built_in=True
        )
        
        child1 = TestDataFactory.create_child(family=self.family, first_name="ראשון")
        child2 = TestDataFactory.create_child(family=self.family, first_name="שני")
        child3 = TestDataFactory.create_child(family=self.family, first_name="שלישי")
        enroll_child_to_team(child1)
        
        # Third child should also get discount
        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(child3.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )
        
        self.assertEqual(result.total_discount_amount, Decimal('50.00'))

    def test_no_enrolled_sibling_no_discount(self):
        """Two kids in the family but none on a team — no discount"""
        Discount.objects.create(
            name="הנחת ילד שני",
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='permanent',
            is_active=True,
            is_built_in=True
        )

        TestDataFactory.create_child(family=self.family, first_name="ראשון")
        child2 = TestDataFactory.create_child(family=self.family, first_name="שני")

        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(child2.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )

        self.assertEqual(result.final_price, Decimal('350.00'))
        self.assertEqual(len(result.applicable_discounts), 0)

    def test_pending_sibling_payment_in_same_checkout_gets_discount(self):
        """Second child in the same widget checkout gets the sibling discount."""
        from apps.customers.models import Payment

        Discount.objects.create(
            name="הנחת ילד שני",
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='permanent',
            is_active=True,
            is_built_in=True,
        )
        child1 = TestDataFactory.create_child(family=self.family, first_name="ראשון")
        child2 = TestDataFactory.create_child(family=self.family, first_name="שני")
        Payment.objects.create(
            child=child1,
            family=self.family,
            branch=self.branch,
            payment_type='recurring_subscription',
            status='pending',
            base_amount=Decimal('350.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('350.00'),
        )

        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(child2.id),
            payment_date=date.today(),
            base_price=Decimal('350.00'),
        )
        self.assertEqual(result.final_price, Decimal('300.00'))
        self.assertEqual(result.total_discount_amount, Decimal('50.00'))

    def test_trial_sibling_does_not_grant_discount(self):
        """Sibling with only a trial-lesson enrollment doesn't grant the discount"""
        Discount.objects.create(
            name="הנחת ילד שני",
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='permanent',
            is_active=True,
            is_built_in=True
        )

        child1 = TestDataFactory.create_child(family=self.family, first_name="ראשון")
        child2 = TestDataFactory.create_child(family=self.family, first_name="שני")
        enroll_child_to_team(child1, trial=True)

        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(child2.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )

        self.assertEqual(result.final_price, Decimal('350.00'))
        self.assertEqual(len(result.applicable_discounts), 0)

    def test_inactive_sibling_enrollment_no_discount(self):
        """Sibling whose team enrollment is inactive doesn't grant the discount"""
        Discount.objects.create(
            name="הנחת ילד שני",
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='permanent',
            is_active=True,
            is_built_in=True
        )

        child1 = TestDataFactory.create_child(family=self.family, first_name="ראשון")
        child2 = TestDataFactory.create_child(family=self.family, first_name="שני")
        enrollment = enroll_child_to_team(child1)
        enrollment.status = 'inactive'
        enrollment.save(update_fields=['status'])

        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(child2.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )

        self.assertEqual(result.final_price, Decimal('350.00'))
        self.assertEqual(len(result.applicable_discounts), 0)


class DiscountServiceMultipleDiscountsTest(TestCase):
    """Test DiscountService combining multiple discounts"""
    
    def setUp(self):
        self.service = DiscountService()
        self.family = TestDataFactory.create_family()
        self.branch = self.family.branch
    
    def test_early_signup_and_second_child_combined(self):
        """Test early signup and second child discounts combine additively"""
        # Create both discounts - must have is_built_in=True and proper identifiers
        early_signup = Discount.objects.create(
            name="הנחת רישום מוקדם",
            discount_type='fixed',
            value=Decimal('75.00'),
            applies_to='child',
            promotion_type='temporary',
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30),
            is_active=True,
            is_built_in=True
        )
        
        second_child = Discount.objects.create(
            name="הנחת ילד שני",
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='permanent',
            is_active=True,
            is_built_in=True
        )
        
        # Create two children; first child signed to a team
        child1 = TestDataFactory.create_child(family=self.family, first_name="ראשון")
        child2 = TestDataFactory.create_child(family=self.family, first_name="שני")
        enroll_child_to_team(child1)
        
        # Second child during early signup period - should get both discounts
        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(child2.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )
        
        # 350 - 75 (early signup) - 50 (second child) = 225
        self.assertEqual(result.final_price, Decimal('225.00'))
        self.assertEqual(result.total_discount_amount, Decimal('125.00'))
        self.assertEqual(len(result.applicable_discounts), 2)
    
    def test_fixed_final_price_overrides_other_discounts(self):
        """Test fixed final price discount overrides additive discounts"""
        # Create fixed final price discount - must have is_built_in=True and identifier
        fixed_price = Discount.objects.create(
            name="מחיר מיוחד - רישום מוקדם",
            discount_type='fixed_final_price',
            value=Decimal('299.00'),
            applies_to='child',
            promotion_type='temporary',
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30),
            is_active=True,
            is_built_in=True
        )
        
        # Create second child discount (should be ignored due to fixed_final_price)
        second_child = Discount.objects.create(
            name="הנחת ילד שני",
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='permanent',
            is_active=True,
            is_built_in=True
        )
        
        child1 = TestDataFactory.create_child(family=self.family, first_name="ראשון")
        child2 = TestDataFactory.create_child(family=self.family, first_name="שני")
        enroll_child_to_team(child1)
        
        # Fixed price should override second child discount
        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(child2.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )
        
        self.assertEqual(result.final_price, Decimal('299.00'))
        self.assertEqual(len(result.applicable_discounts), 1)
        self.assertEqual(result.applicable_discounts[0].discount_type, 'fixed_final_price')


class DiscountServiceNoDiscountsTest(TestCase):
    """Test DiscountService when no discounts apply"""
    
    def setUp(self):
        self.service = DiscountService()
        self.family = TestDataFactory.create_family()
        self.child = TestDataFactory.create_child(family=self.family)
    
    def test_no_discounts_returns_base_price(self):
        """Test returns base price when no discounts apply"""
        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(self.child.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )
        
        self.assertEqual(result.base_price, Decimal('350.00'))
        self.assertEqual(result.final_price, Decimal('350.00'))
        self.assertEqual(result.total_discount_amount, Decimal('0.00'))
        self.assertEqual(len(result.applicable_discounts), 0)
    
    def test_only_first_child_no_discount(self):
        """Test first child with no early signup discount gets no discount"""
        # No discounts created
        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(self.child.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )
        
        self.assertEqual(result.final_price, Decimal('350.00'))
        self.assertEqual(len(result.applicable_discounts), 0)


class DiscountServiceEdgeCasesTest(TestCase):
    """Test DiscountService edge cases"""
    
    def setUp(self):
        self.service = DiscountService()
        self.family = TestDataFactory.create_family()
        self.child = TestDataFactory.create_child(family=self.family)
    
    def test_discount_larger_than_base_price(self):
        """Test discount amount cannot make final price negative"""
        discount = Discount.objects.create(
            name="הנחה גדולה",
            discount_type='fixed',
            value=Decimal('400.00'),  # Larger than base price
            applies_to='child',
            promotion_type='temporary',
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30),
            is_active=True
        )
        
        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(self.child.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )
        
        # Final price should not be negative
        self.assertGreaterEqual(result.final_price, Decimal('0.00'))
    
    def test_discount_calculation_with_zero_base_price(self):
        """Test discount calculation with zero base price"""
        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(self.child.id),
            payment_date=date.today(),
            base_price=Decimal('0.00')
        )
        
        self.assertEqual(result.final_price, Decimal('0.00'))
        self.assertEqual(result.total_discount_amount, Decimal('0.00'))
    
    def test_multiple_early_signup_discounts_only_one_applies(self):
        """Test when multiple early signup discounts exist, only one is selected"""
        discount1 = Discount.objects.create(
            name="הנחת רישום מוקדם 1",
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='temporary',
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30),
            is_active=True,
            is_built_in=True
        )
        
        discount2 = Discount.objects.create(
            name="הנחת רישום מוקדם 2",
            discount_type='fixed',
            value=Decimal('75.00'),
            applies_to='child',
            promotion_type='temporary',
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30),
            is_active=True,
            is_built_in=True
        )
        
        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(self.child.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )
        
        # Should only have one early signup discount applied
        early_signup_discounts = [d for d in result.applicable_discounts if 'רישום מוקדם' in d.name]
        self.assertLessEqual(len(early_signup_discounts), 1)


class AnotherClassAmountOffTest(TestCase):
    """
    The another-class discount said as an amount off (owner, 8.10.2026): "הנחה
    של 10 שקלים מכל סוגי החוגים", and a parent with a brother AND another class
    gets both. Said as a price ("מחיר קבוע לחוג נוסף") it stays what it was: final.
    """

    NAME = 'הנחת שיעור נוסף לילד פעיל'

    def setUp(self):
        self.service = DiscountService()
        self.family = TestDataFactory.create_family()
        self.child = TestDataFactory.create_child(family=self.family, first_name='ראשון', status='active')
        self.first_lesson = TestDataFactory.create_lesson()
        self.second_lesson = TestDataFactory.create_lesson()
        enroll_child_to_team(self.child, lesson=self.first_lesson)

    def another_class(self, kind, value):
        return Discount.objects.create(
            name=self.NAME, discount_type=kind, value=Decimal(value), applies_to='child',
            promotion_type='permanent', is_active=True, is_built_in=True,
        )

    def second_child(self, value='10.00'):
        brother = TestDataFactory.create_child(family=self.family, first_name='אח', status='active')
        enroll_child_to_team(brother)
        return Discount.objects.create(
            name='הנחת ילד שני', discount_type='fixed', value=Decimal(value), applies_to='child',
            promotion_type='permanent', is_active=True, is_built_in=True,
        )

    def price(self, base='350.00', lesson='second'):
        return self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id), child_id=str(self.child.id), payment_date=date.today(),
            base_price=Decimal(base), lesson_id=str(self.second_lesson.id if lesson == 'second' else self.first_lesson.id),
        )

    def test_ten_shekels_off_the_second_class(self):
        self.another_class('fixed', '10.00')

        result = self.price()

        self.assertEqual((result.final_price, result.total_discount_amount), (Decimal('340.00'), Decimal('10.00')))
        line, = result.applicable_discounts
        self.assertEqual((line.discount_type, line.name, line.value), ('additional_lesson', self.NAME, Decimal('10.00')))

    def test_the_same_ten_off_a_cheaper_and_a_dearer_class(self):
        self.another_class('fixed', '10.00')

        self.assertEqual(self.price('260.00').final_price, Decimal('250.00'))
        self.assertEqual(self.price('455.00').final_price, Decimal('445.00'))

    def test_the_first_class_stays_at_full_price(self):
        self.another_class('fixed', '10.00')

        result = self.price(lesson='first')

        self.assertEqual(result.final_price, Decimal('350.00'))
        self.assertEqual(result.applicable_discounts, [])

    def test_a_brother_and_another_class_are_two_discounts(self):
        self.another_class('fixed', '10.00')
        self.second_child('10.00')

        result = self.price()

        self.assertEqual((result.final_price, result.total_discount_amount), (Decimal('330.00'), Decimal('20.00')))
        self.assertEqual(
            sorted(line.discount_type for line in result.applicable_discounts), ['additional_lesson', 'second_child'],
        )

    def test_said_as_a_price_it_is_final_and_the_brother_does_not_come_off_it(self):
        """The older way, unchanged: "מחיר קבוע לחוג נוסף" is the price."""
        self.another_class('fixed_final_price', '300.00')
        self.second_child('10.00')

        result = self.price()

        self.assertEqual((result.final_price, result.total_discount_amount), (Decimal('300.00'), Decimal('50.00')))
        self.assertEqual([line.discount_type for line in result.applicable_discounts], ['additional_lesson'])

    def test_a_percentage_off_works_the_same_way(self):
        self.another_class('percentage', '10')

        result = self.price()

        self.assertEqual(result.final_price, Decimal('315.00'))

    def test_never_more_off_than_the_class_costs(self):
        self.another_class('fixed', '10.00')

        result = self.price('6.00')

        self.assertEqual((result.final_price, result.total_discount_amount), (Decimal('0.00'), Decimal('6.00')))

    def test_a_row_billed_at_nothing_carries_no_discount_line(self):
        """The extra days of a track are billed at ₪0: there is nothing to take ten shekels off."""
        self.another_class('fixed', '10.00')

        result = self.price('0.00')

        self.assertEqual(result.applicable_discounts, [])
        self.assertEqual(result.final_price, Decimal('0.00'))

    def test_switched_off_or_left_at_zero_it_is_not_a_discount(self):
        row = self.another_class('fixed', '0.00')
        self.assertEqual(self.price().final_price, Decimal('350.00'))

        row.value, row.is_active = Decimal('10.00'), False
        row.save(update_fields=['value', 'is_active'])
        self.assertEqual(self.price().final_price, Decimal('350.00'))


class AnotherClassSettingsTest(TestCase):
    """The settings door: the discount carries its kind, and an older screen changes nothing."""

    URL = '/api/v1/customers/discounts/additional-lesson/'

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        manager = TestDataFactory.create_user(username='discount-manager@kogo.test')
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.get(pk=manager.pk))

    def test_it_starts_as_a_price_nobody_set(self):
        answer = self.client.get(self.URL)

        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual((answer.data['discount_type'], Decimal(str(answer.data['value']))), ('fixed_final_price', Decimal('0')))

    def test_the_office_says_ten_shekels_off(self):
        self.client.get(self.URL)

        saved = self.client.put(self.URL, {'discount_type': 'fixed', 'value': '10.00', 'is_active': True}, format='json')

        self.assertEqual(saved.status_code, 200, saved.content)
        row = Discount.objects.get(name__contains='שיעור נוסף')
        self.assertEqual((row.discount_type, row.value, row.is_active), ('fixed', Decimal('10.00'), True))
        self.assertEqual(self.client.get(self.URL).data['discount_type'], 'fixed')

    def test_and_back_to_a_price(self):
        self.client.get(self.URL)
        self.client.put(self.URL, {'discount_type': 'fixed', 'value': '10.00'}, format='json')

        saved = self.client.put(self.URL, {'discount_type': 'fixed_final_price', 'value': '300.00'}, format='json')

        self.assertEqual(saved.status_code, 200, saved.content)
        row = Discount.objects.get(name__contains='שיעור נוסף')
        self.assertEqual((row.discount_type, row.value), ('fixed_final_price', Decimal('300.00')))

    def test_a_screen_that_does_not_send_the_kind_leaves_it_alone(self):
        self.client.get(self.URL)
        self.client.put(self.URL, {'discount_type': 'fixed', 'value': '10.00'}, format='json')

        saved = self.client.put(self.URL, {'value': '15.00', 'is_active': True}, format='json')

        self.assertEqual(saved.status_code, 200, saved.content)
        row = Discount.objects.get(name__contains='שיעור נוסף')
        self.assertEqual((row.discount_type, row.value), ('fixed', Decimal('15.00')))

    def test_nothing_and_a_kind_nobody_offers_are_refused(self):
        self.client.get(self.URL)

        self.assertEqual(self.client.put(self.URL, {'discount_type': 'fixed', 'value': '0'}, format='json').status_code, 400)
        self.assertEqual(self.client.put(self.URL, {'discount_type': 'free', 'value': '10'}, format='json').status_code, 400)
