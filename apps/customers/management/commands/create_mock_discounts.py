"""
Create mock discount data for testing the discount visualization
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from decimal import Decimal
from datetime import timedelta
from apps.customers.models import Payment, Child, Family, PaymentDiscountSnapshot
from apps.core.models import Branch


class Command(BaseCommand):
    help = 'Create mock discount data for testing discount visualization'

    def handle(self, *args, **kwargs):
        # Get first branch and first child for test data
        branch = Branch.objects.first()
        if not branch:
            self.stdout.write(self.style.ERROR('No branches found. Please create a branch first.'))
            return
        
        family = Family.objects.first()
        if not family:
            self.stdout.write(self.style.ERROR('No families found. Please create a family first.'))
            return
        
        child = Child.objects.first()
        if not child:
            self.stdout.write(self.style.ERROR('No children found. Please create a child first.'))
            return
        
        self.stdout.write('Creating mock payments and discounts...')
        
        # Create mock payments with different discount types
        mock_data = [
            {
                'base_amount': Decimal('400.00'),
                'discount_amount': Decimal('50.00'),
                'final_amount': Decimal('350.00'),
                'discounts': [
                    {
                        'name': 'הנחת רישום מוקדם',
                        'type': 'early_signup',
                        'value': Decimal('50.00'),
                        'reason': 'רישום בתקופת המבצע'
                    }
                ]
            },
            {
                'base_amount': Decimal('400.00'),
                'discount_amount': Decimal('100.00'),
                'final_amount': Decimal('300.00'),
                'discounts': [
                    {
                        'name': 'הנחת ילד שני',
                        'type': 'second_child',
                        'value': Decimal('100.00'),
                        'reason': 'ילד שני במשפחה'
                    }
                ]
            },
            {
                'base_amount': Decimal('400.00'),
                'discount_amount': Decimal('300.00'),
                'final_amount': Decimal('100.00'),
                'discounts': [
                    {
                        'name': 'הנחת שיעור נוסף',
                        'type': 'additional_lesson',
                        'value': Decimal('300.00'),
                        'reason': 'שיעור נוסף - מחיר קבוע 100 ₪'
                    }
                ]
            },
            {
                'base_amount': Decimal('500.00'),
                'discount_amount': Decimal('400.00'),
                'final_amount': Decimal('100.00'),
                'discounts': [
                    {
                        'name': 'מחיר קבוע מיוחד',
                        'type': 'fixed_final_price',
                        'value': Decimal('400.00'),
                        'reason': 'מחיר קבוע 100 ₪ לשיעור'
                    }
                ]
            },
            {
                'base_amount': Decimal('400.00'),
                'discount_amount': Decimal('75.00'),
                'final_amount': Decimal('325.00'),
                'discounts': [
                    {
                        'name': 'הנחת רישום מוקדם',
                        'type': 'early_signup',
                        'value': Decimal('75.00'),
                        'reason': 'רישום בתקופת המבצע'
                    }
                ]
            },
            {
                'base_amount': Decimal('350.00'),
                'discount_amount': Decimal('100.00'),
                'final_amount': Decimal('250.00'),
                'discounts': [
                    {
                        'name': 'הנחת ילד שני',
                        'type': 'second_child',
                        'value': Decimal('100.00'),
                        'reason': 'ילד שני במשפחה'
                    }
                ]
            },
        ]
        
        created_payments = 0
        created_snapshots = 0
        
        for data in mock_data:
            # Create payment
            payment = Payment.objects.create(
                child=child,
                family=family,
                branch=branch,
                payment_type='recurring_subscription',
                status='completed',
                base_amount=data['base_amount'],
                discount_amount=data['discount_amount'],
                final_amount=data['final_amount'],
                description='תשלום בדיקה - מוק',
                payment_date=timezone.now() - timedelta(days=created_payments * 2)
            )
            created_payments += 1
            
            # Create discount snapshots
            for discount in data['discounts']:
                PaymentDiscountSnapshot.objects.create(
                    payment=payment,
                    discount_name=discount['name'],
                    discount_type=discount['type'],
                    discount_value=discount['value'],
                    amount_deducted=discount['value'],
                    reason=discount['reason']
                )
                created_snapshots += 1
        
        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully created {created_payments} mock payments '
                f'with {created_snapshots} discount snapshots'
            )
        )
        self.stdout.write(
            self.style.WARNING(
                'Note: These are test payments. You can delete them from the admin panel when done testing.'
            )
        )
