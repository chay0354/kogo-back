"""
Management command to setup built-in discounts

Usage:
    python manage.py setup_discounts
    
This command will:
1. Create the Second Child Discount (if not exists)
2. Create an example Early Sign-Up discount range (if not exists)
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
from apps.customers.financial_models import Discount


class Command(BaseCommand):
    help = 'Setup built-in discounts (Second Child and Example Early Sign-Up)'
    
    def handle(self, *args, **options):
        self.stdout.write('Setting up built-in discounts...')
        
        # 1. Setup Second Child Discount
        second_child, created = Discount.objects.get_or_create(
            name='הנחת ילד שני',
            is_built_in=True,
            defaults={
                'description': 'הנחה אוטומטית לילד שני ומעלה במשפחה',
                'discount_type': 'fixed',
                'value': Decimal('50.00'),  # Default 50 NIS discount
                'applies_to': 'child',
                'promotion_type': 'permanent',
                'is_active': True
            }
        )
        
        if created:
            self.stdout.write(
                self.style.SUCCESS(
                    f'✓ Created Second Child Discount: {second_child.value} ₪'
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f'○ Second Child Discount already exists: {second_child.value} ₪'
                )
            )
        
        # 2. Setup Example Early Sign-Up Discount
        # Create a date range for next month as an example
        today = timezone.now().date()
        next_month_start = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        next_month_end = (next_month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        
        # Check if any early signup discounts exist
        existing_early_signup = Discount.objects.filter(
            is_built_in=True,
            name__contains='רישום מוקדם'
        ).exists()
        
        if not existing_early_signup:
            example_discount = Discount.objects.create(
                name=f'הנחת רישום מוקדם {next_month_start.strftime("%d/%m/%Y")} - {next_month_end.strftime("%d/%m/%Y")}',
                description='הנחה לתשלומים שנעשו בטווח תאריכים זה',
                discount_type='fixed',
                value=Decimal('100.00'),  # Example: 100 NIS discount
                applies_to='family',
                promotion_type='temporary',
                start_date=next_month_start,
                end_date=next_month_end,
                is_built_in=True,
                is_active=True
            )
            
            self.stdout.write(
                self.style.SUCCESS(
                    f'✓ Created Example Early Sign-Up Discount: {example_discount.value} ₪ '
                    f'({example_discount.start_date} to {example_discount.end_date})'
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    '○ Early Sign-Up Discounts already exist, skipping example creation'
                )
            )
        
        self.stdout.write(
            self.style.SUCCESS('\n✓ Discount setup complete!')
        )
        self.stdout.write('\nYou can now:')
        self.stdout.write('  - View discounts at: /discounts')
        self.stdout.write('  - Modify second child discount value')
        self.stdout.write('  - Add more early sign-up date ranges')

