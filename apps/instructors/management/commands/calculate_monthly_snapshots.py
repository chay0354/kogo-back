from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from apps.instructors.utils import generate_monthly_snapshots


class Command(BaseCommand):
    help = 'Calculate monthly snapshots for instructors, lessons, and branches'

    def add_arguments(self, parser):
        parser.add_argument(
            '--month',
            type=str,
            help='Month to calculate in YYYY-MM format (defaults to previous month)',
        )
        parser.add_argument(
            '--current',
            action='store_true',
            help='Calculate for current month (default is previous month)',
        )

    def handle(self, *args, **options):
        # Determine which month to calculate
        if options['month']:
            month_str = options['month']
            # Validate format
            try:
                year, month = month_str.split('-')
                year, month = int(year), int(month)
                if not (1 <= month <= 12):
                    raise ValueError
                month_str = f"{year}-{month:02d}"
            except (ValueError, AttributeError):
                self.stdout.write(
                    self.style.ERROR('Invalid month format. Use YYYY-MM format (e.g., 2025-01)')
                )
                return
        elif options['current']:
            # Current month
            today = timezone.now().date()
            month_str = today.strftime('%Y-%m')
        else:
            # Default to previous month
            today = timezone.now().date()
            first_of_month = today.replace(day=1)
            last_month = first_of_month - timedelta(days=1)
            month_str = last_month.strftime('%Y-%m')

        self.stdout.write(f'Calculating monthly snapshots for {month_str}...')
        self.stdout.write('')

        try:
            result = generate_monthly_snapshots(month_str)
            
            self.stdout.write(self.style.SUCCESS(f'✓ Instructor snapshots: {result["instructors_created"]} created/updated'))
            self.stdout.write(self.style.SUCCESS(f'✓ Lesson snapshots: {result["lessons_created"]} created/updated'))
            self.stdout.write(self.style.SUCCESS(f'✓ Branch snapshots: {result["branches_created"]} created/updated'))
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS(f'Successfully calculated snapshots for {month_str}'))
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error calculating snapshots: {str(e)}'))
            import traceback
            traceback.print_exc()
            raise

