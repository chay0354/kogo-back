"""
Revenue Service - Centralized revenue calculations based on actual payments

This service provides accurate revenue calculations based on completed payments
(Payment.final_amount) rather than theoretical lesson prices. This ensures
revenue reports reflect actual money collected, accounting for discounts.

Usage:
    service = RevenueService()
    revenue = service.get_branch_revenue(branch_id, start_date, end_date)
    discounts = service.get_branch_discounts(branch_id, start_date, end_date)
"""
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from django.db.models import Sum, Count, Q, F
from django.db.models.functions import TruncMonth

from apps.customers.models import Payment, RecurringPayment
from apps.customers.financial_models import BranchDiscountMetrics
from apps.core.models import Branch

logger = logging.getLogger(__name__)


class RevenueService:
    """Service for calculating revenue based on actual collected payments"""
    
    def get_branch_revenue(
        self,
        branch_id: str,
        start_date: date,
        end_date: date
    ) -> Decimal:
        """
        Get actual collected revenue for a branch in a date range.
        
        This sums Payment.final_amount for all completed payments,
        which represents the real money collected after discounts.
        
        Args:
            branch_id: UUID of branch
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
            
        Returns:
            Total revenue as Decimal
        """
        result = Payment.objects.filter(
            branch_id=branch_id,
            status='completed',
            payment_date__date__gte=start_date,
            payment_date__date__lte=end_date
        ).aggregate(total=Sum('final_amount'))
        
        return result['total'] or Decimal('0.00')
    
    def get_branch_discounts(
        self,
        branch_id: str,
        start_date: date,
        end_date: date
    ) -> Decimal:
        """
        Get total discounts given by a branch in a date range.
        
        Args:
            branch_id: UUID of branch
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
            
        Returns:
            Total discount amount as Decimal
        """
        result = Payment.objects.filter(
            branch_id=branch_id,
            status='completed',
            payment_date__date__gte=start_date,
            payment_date__date__lte=end_date
        ).aggregate(total=Sum('discount_amount'))
        
        return result['total'] or Decimal('0.00')
    
    def get_branch_base_revenue(
        self,
        branch_id: str,
        start_date: date,
        end_date: date
    ) -> Decimal:
        """
        Get theoretical revenue (before discounts) for a branch.
        
        This sums Payment.base_amount to show what revenue would have been
        without any discounts applied.
        
        Args:
            branch_id: UUID of branch
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
            
        Returns:
            Base revenue as Decimal
        """
        result = Payment.objects.filter(
            branch_id=branch_id,
            status='completed',
            payment_date__date__gte=start_date,
            payment_date__date__lte=end_date
        ).aggregate(total=Sum('base_amount'))
        
        return result['total'] or Decimal('0.00')
    
    def get_instructor_revenue(
        self,
        instructor_id: str,
        start_date: date,
        end_date: date
    ) -> Decimal:
        """
        Get actual revenue for an instructor's lessons in a date range.
        
        Args:
            instructor_id: UUID of instructor
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
            
        Returns:
            Total revenue as Decimal
        """
        result = Payment.objects.filter(
            lesson__instructor_id=instructor_id,
            status='completed',
            payment_date__date__gte=start_date,
            payment_date__date__lte=end_date
        ).aggregate(total=Sum('final_amount'))
        
        return result['total'] or Decimal('0.00')
    
    def get_lesson_revenue(
        self,
        lesson_id: str,
        start_date: date,
        end_date: date
    ) -> Decimal:
        """
        Get actual revenue for a specific lesson in a date range.
        
        Args:
            lesson_id: UUID of lesson
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
            
        Returns:
            Total revenue as Decimal
        """
        result = Payment.objects.filter(
            lesson_id=lesson_id,
            status='completed',
            payment_date__date__gte=start_date,
            payment_date__date__lte=end_date
        ).aggregate(total=Sum('final_amount'))
        
        return result['total'] or Decimal('0.00')
    
    def get_revenue_breakdown(
        self,
        branch_id: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None
    ) -> Dict:
        """
        Get comprehensive revenue breakdown with actual vs theoretical.
        
        Args:
            branch_id: Optional branch filter
            start_date: Optional start date
            end_date: Optional end date
            
        Returns:
            Dict with revenue metrics
        """
        filters = Q(status='completed')
        
        if branch_id:
            filters &= Q(branch_id=branch_id)
        if start_date:
            filters &= Q(payment_date__date__gte=start_date)
        if end_date:
            filters &= Q(payment_date__date__lte=end_date)
        
        result = Payment.objects.filter(filters).aggregate(
            actual_revenue=Sum('final_amount'),
            base_revenue=Sum('base_amount'),
            total_discounts=Sum('discount_amount'),
            payment_count=Count('id')
        )
        
        actual_revenue = result['actual_revenue'] or Decimal('0.00')
        base_revenue = result['base_revenue'] or Decimal('0.00')
        total_discounts = result['total_discounts'] or Decimal('0.00')
        
        # Calculate discount percentage
        discount_percentage = 0
        if base_revenue > 0:
            discount_percentage = float((total_discounts / base_revenue) * 100)
        
        return {
            'actual_revenue': actual_revenue,
            'base_revenue': base_revenue,
            'total_discounts': total_discounts,
            'discount_percentage': round(discount_percentage, 2),
            'payment_count': result['payment_count'] or 0
        }
    
    def get_monthly_revenue_trend(
        self,
        branch_id: Optional[str] = None,
        months: int = 12
    ) -> List[Dict]:
        """
        Get monthly revenue trend for the past N months.
        
        Args:
            branch_id: Optional branch filter
            months: Number of months to include (default 12)
            
        Returns:
            List of dicts with month, actual_revenue, base_revenue, discounts
        """
        filters = Q(status='completed')
        if branch_id:
            filters &= Q(branch_id=branch_id)
        
        # Get payments grouped by month
        monthly_data = (
            Payment.objects
            .filter(filters)
            .annotate(month=TruncMonth('payment_date'))
            .values('month')
            .annotate(
                actual_revenue=Sum('final_amount'),
                base_revenue=Sum('base_amount'),
                discounts=Sum('discount_amount'),
                count=Count('id')
            )
            .order_by('-month')[:months]
        )
        
        result = []
        for item in monthly_data:
            result.append({
                'month': item['month'].strftime('%Y-%m') if item['month'] else '',
                'actual_revenue': float(item['actual_revenue'] or 0),
                'base_revenue': float(item['base_revenue'] or 0),
                'discounts': float(item['discounts'] or 0),
                'payment_count': item['count']
            })
        
        return list(reversed(result))  # Return oldest to newest
    
    def update_branch_discount_metrics(
        self,
        branch_id: str,
        month: date
    ) -> BranchDiscountMetrics:
        """
        Update or create discount metrics for a branch and month.
        
        This aggregates all discount data from completed payments in the month
        and stores it in BranchDiscountMetrics for fast dashboard queries.
        
        Args:
            branch_id: UUID of branch
            month: First day of the month
            
        Returns:
            Updated BranchDiscountMetrics instance
        """
        # Ensure month is first day of month
        month = month.replace(day=1)
        
        # Calculate month boundaries
        if month.month == 12:
            next_month = month.replace(year=month.year + 1, month=1)
        else:
            next_month = month.replace(month=month.month + 1)
        
        # Aggregate discount data for the month
        payments = Payment.objects.filter(
            branch_id=branch_id,
            status='completed',
            payment_date__date__gte=month,
            payment_date__date__lt=next_month
        )
        
        totals = payments.aggregate(
            total_discount_amount=Sum('discount_amount'),
            discount_count=Count('id', filter=Q(discount_amount__gt=0))
        )
        
        # Calculate breakdown by discount type (from snapshots)
        from apps.customers.models import PaymentDiscountSnapshot
        
        snapshots = PaymentDiscountSnapshot.objects.filter(
            payment__in=payments
        )
        
        early_signup_total = snapshots.filter(
            discount_type='early_signup'
        ).aggregate(total=Sum('amount_deducted'))['total'] or Decimal('0.00')
        
        second_child_total = snapshots.filter(
            discount_type='second_child'
        ).aggregate(total=Sum('amount_deducted'))['total'] or Decimal('0.00')
        
        fixed_price_total = snapshots.filter(
            discount_type='fixed_final_price'
        ).aggregate(total=Sum('amount_deducted'))['total'] or Decimal('0.00')
        
        # Update or create metrics
        metrics, created = BranchDiscountMetrics.objects.update_or_create(
            branch_id=branch_id,
            month=month,
            defaults={
                'total_discount_amount': totals['total_discount_amount'] or Decimal('0.00'),
                'discount_count': totals['discount_count'] or 0,
                'early_signup_total': early_signup_total,
                'second_child_total': second_child_total,
                'fixed_price_total': fixed_price_total
            }
        )
        
        logger.info(
            f"Updated discount metrics for {branch_id} {month.strftime('%Y-%m')}: "
            f"₪{metrics.total_discount_amount} ({metrics.discount_count} discounts)"
        )
        
        return metrics
    
    def get_discount_metrics(
        self,
        branch_id: str,
        start_date: date,
        end_date: date
    ) -> Dict:
        """
        Get aggregated discount metrics for a branch and date range.
        
        Args:
            branch_id: UUID of branch
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
            
        Returns:
            Dict with discount breakdown
        """
        # Get all months in the range
        metrics = BranchDiscountMetrics.objects.filter(
            branch_id=branch_id,
            month__gte=start_date.replace(day=1),
            month__lte=end_date.replace(day=1)
        ).aggregate(
            total_discount_amount=Sum('total_discount_amount'),
            discount_count=Sum('discount_count'),
            early_signup_total=Sum('early_signup_total'),
            second_child_total=Sum('second_child_total'),
            fixed_price_total=Sum('fixed_price_total')
        )
        
        return {
            'total_discount_amount': float(metrics['total_discount_amount'] or 0),
            'discount_count': metrics['discount_count'] or 0,
            'breakdown': {
                'early_signup': float(metrics['early_signup_total'] or 0),
                'second_child': float(metrics['second_child_total'] or 0),
                'fixed_price': float(metrics['fixed_price_total'] or 0)
            }
        }
