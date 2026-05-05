"""
Unit tests for RevenueService.

Tests coverage:
- get_branch_revenue: revenue calculations by branch
- get_branch_discounts: discount aggregation
- get_branch_base_revenue: pre-discount revenue
- get_instructor_revenue: revenue by instructor
- get_lesson_revenue: revenue by lesson
- Date range filtering
"""
from decimal import Decimal
from datetime import date, timedelta
from django.test import TestCase
from django.utils import timezone

from apps.core.tests.test_fixtures import TestDataFactory
from apps.core.revenue_service import RevenueService
from apps.customers.models import Payment


class RevenueServiceBranchRevenueTest(TestCase):
    """Test RevenueService branch revenue calculations"""
    
    def setUp(self):
        self.service = RevenueService()
        self.branch = TestDataFactory.create_branch()
        self.child = TestDataFactory.create_child()
        self.lesson = TestDataFactory.create_lesson(branch=self.branch)
    
    def test_get_branch_revenue_with_completed_payments(self):
        """Test branch revenue calculation with completed payments"""
        # Create completed payments
        payment1 = Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('350.00'),
            discount_amount=Decimal('50.00'),
            final_amount=Decimal('300.00'),
            payment_date=timezone.now()
        )
        
        payment2 = Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('350.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('350.00'),
            payment_date=timezone.now()
        )
        
        result = self.service.get_branch_revenue(
            branch_id=str(self.branch.id),
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1)
        )
        
        # 300 + 350 = 650
        self.assertEqual(result, Decimal('650.00'))
    
    def test_get_branch_revenue_excludes_pending_payments(self):
        """Test branch revenue excludes pending payments"""
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='pending',
            base_amount=Decimal('350.00'),
            final_amount=Decimal('350.00'),
            payment_date=timezone.now()
        )
        
        result = self.service.get_branch_revenue(
            branch_id=str(self.branch.id),
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1)
        )
        
        self.assertEqual(result, Decimal('0.00'))
    
    def test_get_branch_revenue_date_range_filter(self):
        """Test branch revenue respects date range"""
        # Payment within range
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('350.00'),
            final_amount=Decimal('350.00'),
            payment_date=timezone.now()
        )
        
        # Payment outside range
        past_date = timezone.now() - timedelta(days=60)
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('400.00'),
            final_amount=Decimal('400.00'),
            payment_date=past_date
        )
        
        # Query for current month only
        result = self.service.get_branch_revenue(
            branch_id=str(self.branch.id),
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1)
        )
        
        # Should only include current payment
        self.assertEqual(result, Decimal('350.00'))
    
    def test_get_branch_revenue_no_payments(self):
        """Test branch revenue returns zero when no payments"""
        result = self.service.get_branch_revenue(
            branch_id=str(self.branch.id),
            start_date=date.today() - timedelta(days=30),
            end_date=date.today()
        )
        
        self.assertEqual(result, Decimal('0.00'))


class RevenueServiceDiscountsTest(TestCase):
    """Test RevenueService discount calculations"""
    
    def setUp(self):
        self.service = RevenueService()
        self.branch = TestDataFactory.create_branch()
        self.child = TestDataFactory.create_child()
        self.lesson = TestDataFactory.create_lesson(branch=self.branch)
    
    def test_get_branch_discounts(self):
        """Test branch discount calculation"""
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('350.00'),
            discount_amount=Decimal('50.00'),
            final_amount=Decimal('300.00'),
            payment_date=timezone.now()
        )
        
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('350.00'),
            discount_amount=Decimal('75.00'),
            final_amount=Decimal('275.00'),
            payment_date=timezone.now()
        )
        
        result = self.service.get_branch_discounts(
            branch_id=str(self.branch.id),
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1)
        )
        
        # 50 + 75 = 125
        self.assertEqual(result, Decimal('125.00'))
    
    def test_get_branch_base_revenue(self):
        """Test branch base revenue (pre-discount) calculation"""
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('350.00'),
            discount_amount=Decimal('50.00'),
            final_amount=Decimal('300.00'),
            payment_date=timezone.now()
        )
        
        result = self.service.get_branch_base_revenue(
            branch_id=str(self.branch.id),
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1)
        )
        
        self.assertEqual(result, Decimal('350.00'))


class RevenueServiceInstructorRevenueTest(TestCase):
    """Test RevenueService instructor revenue calculations"""
    
    def setUp(self):
        self.service = RevenueService()
        self.branch = TestDataFactory.create_branch()
        self.instructor = TestDataFactory.create_instructor(branch=self.branch)
        self.child = TestDataFactory.create_child()
        self.lesson = TestDataFactory.create_lesson(
            branch=self.branch,
            instructor=self.instructor
        )
    
    def test_get_instructor_revenue(self):
        """Test instructor revenue calculation"""
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('350.00'),
            final_amount=Decimal('350.00'),
            payment_date=timezone.now()
        )
        
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('350.00'),
            final_amount=Decimal('300.00'),
            payment_date=timezone.now()
        )
        
        result = self.service.get_instructor_revenue(
            instructor_id=str(self.instructor.id),
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1)
        )
        
        # 350 + 300 = 650
        self.assertEqual(result, Decimal('650.00'))
    
    def test_get_instructor_revenue_multiple_instructors(self):
        """Test instructor revenue only counts specific instructor's payments"""
        # Create another instructor and lesson
        instructor2 = TestDataFactory.create_instructor(
            first_name="שרה",
            last_name="לוי",
            branch=self.branch
        )
        lesson2 = TestDataFactory.create_lesson(
            branch=self.branch,
            instructor=instructor2
        )
        
        # Payment for instructor 1
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('350.00'),
            final_amount=Decimal('350.00'),
            payment_date=timezone.now()
        )
        
        # Payment for instructor 2
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=lesson2,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('400.00'),
            final_amount=Decimal('400.00'),
            payment_date=timezone.now()
        )
        
        result = self.service.get_instructor_revenue(
            instructor_id=str(self.instructor.id),
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1)
        )
        
        # Should only count instructor 1's payment
        self.assertEqual(result, Decimal('350.00'))


class RevenueServiceLessonRevenueTest(TestCase):
    """Test RevenueService lesson revenue calculations"""
    
    def setUp(self):
        self.service = RevenueService()
        self.branch = TestDataFactory.create_branch()
        self.child = TestDataFactory.create_child()
        self.lesson = TestDataFactory.create_lesson(branch=self.branch)
    
    def test_get_lesson_revenue(self):
        """Test lesson revenue calculation"""
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('350.00'),
            final_amount=Decimal('350.00'),
            payment_date=timezone.now()
        )
        
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('300.00'),
            final_amount=Decimal('300.00'),
            payment_date=timezone.now()
        )
        
        result = self.service.get_lesson_revenue(
            lesson_id=str(self.lesson.id),
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1)
        )
        
        # 350 + 300 = 650
        self.assertEqual(result, Decimal('650.00'))
    
    def test_get_lesson_revenue_multiple_lessons(self):
        """Test lesson revenue only counts specific lesson's payments"""
        lesson2 = TestDataFactory.create_lesson(branch=self.branch)
        
        # Payment for lesson 1
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('350.00'),
            final_amount=Decimal('350.00'),
            payment_date=timezone.now()
        )
        
        # Payment for lesson 2
        Payment.objects.create(
            child=self.child,
            family=self.child.family,
            branch=self.branch,
            lesson=lesson2,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('400.00'),
            final_amount=Decimal('400.00'),
            payment_date=timezone.now()
        )
        
        result = self.service.get_lesson_revenue(
            lesson_id=str(self.lesson.id),
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1)
        )
        
        # Should only count lesson 1's payment
        self.assertEqual(result, Decimal('350.00'))


class RevenueServiceEdgeCasesTest(TestCase):
    """Test RevenueService edge cases"""
    
    def setUp(self):
        self.service = RevenueService()
        self.branch = TestDataFactory.create_branch()
    
    def test_revenue_with_failed_payments(self):
        """Test revenue excludes failed payments"""
        child = TestDataFactory.create_child()
        lesson = TestDataFactory.create_lesson(branch=self.branch)
        
        Payment.objects.create(
            child=child,
            family=child.family,
            branch=self.branch,
            lesson=lesson,
            payment_type='recurring_subscription',
            status='failed',
            base_amount=Decimal('350.00'),
            final_amount=Decimal('350.00'),
            payment_date=timezone.now()
        )
        
        result = self.service.get_branch_revenue(
            branch_id=str(self.branch.id),
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1)
        )
        
        self.assertEqual(result, Decimal('0.00'))
    
    def test_revenue_with_refunded_payments(self):
        """Test revenue with refunded payments"""
        child = TestDataFactory.create_child()
        lesson = TestDataFactory.create_lesson(branch=self.branch)
        
        # Completed payment
        Payment.objects.create(
            child=child,
            family=child.family,
            branch=self.branch,
            lesson=lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('350.00'),
            final_amount=Decimal('350.00'),
            payment_date=timezone.now()
        )
        
        # Refunded payment (still counted as completed unless status changes)
        Payment.objects.create(
            child=child,
            family=child.family,
            branch=self.branch,
            lesson=lesson,
            payment_type='recurring_subscription',
            status='refunded',
            base_amount=Decimal('350.00'),
            final_amount=Decimal('350.00'),
            payment_date=timezone.now()
        )
        
        result = self.service.get_branch_revenue(
            branch_id=str(self.branch.id),
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1)
        )
        
        # Should only count completed payments
        self.assertEqual(result, Decimal('350.00'))
    
    def test_revenue_calculations_with_zero_amounts(self):
        """Test revenue calculations handle zero amounts"""
        child = TestDataFactory.create_child()
        lesson = TestDataFactory.create_lesson(branch=self.branch)
        
        Payment.objects.create(
            child=child,
            family=child.family,
            branch=self.branch,
            lesson=lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('0.00'),
            final_amount=Decimal('0.00'),
            payment_date=timezone.now()
        )
        
        result = self.service.get_branch_revenue(
            branch_id=str(self.branch.id),
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1)
        )
        
        self.assertEqual(result, Decimal('0.00'))
