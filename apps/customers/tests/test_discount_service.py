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
        """Test second child discount is applied to 2nd child"""
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
        
        # Create two children
        child1 = TestDataFactory.create_child(family=self.family, first_name="ראשון")
        child2 = TestDataFactory.create_child(family=self.family, first_name="שני")
        
        # First child - no discount
        result1 = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(child1.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )
        
        self.assertEqual(result1.final_price, Decimal('350.00'))
        self.assertEqual(len(result1.applicable_discounts), 0)
        
        # Second child - discount applied
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
        
        # Third child should also get discount
        result = self.service.evaluate_discounts_for_payment(
            family_id=str(self.family.id),
            child_id=str(child3.id),
            payment_date=date.today(),
            base_price=Decimal('350.00')
        )
        
        self.assertEqual(result.total_discount_amount, Decimal('50.00'))


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
        
        # Create two children
        child1 = TestDataFactory.create_child(family=self.family, first_name="ראשון")
        child2 = TestDataFactory.create_child(family=self.family, first_name="שני")
        
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
