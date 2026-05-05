from django.core.management.base import BaseCommand
from django.utils import timezone
from apps.instructors.models import Instructor
from apps.core.models import InstructorMonthlySnapshot
from datetime import datetime, timedelta
from decimal import Decimal

from apps.instructors.utils import (
    calculate_instructor_salary_for_month,
    calculate_instructor_revenue_for_month,
)


class Command(BaseCommand):
    help = 'Finalize monthly salaries for all instructors for the previous month'

    def add_arguments(self, parser):
        parser.add_argument(
            '--year',
            type=int,
            help='Year to finalize (defaults to last month)',
        )
        parser.add_argument(
            '--month',
            type=int,
            help='Month to finalize (defaults to last month)',
        )
        parser.add_argument(
            '--instructor-id',
            type=str,
            help='Specific instructor ID to finalize (optional)',
        )

    def handle(self, *args, **options):
        # Determine which month to finalize
        if options['year'] and options['month']:
            year = options['year']
            month = options['month']
        else:
            # Default to previous month
            today = timezone.now().date()
            first_of_month = today.replace(day=1)
            last_month = first_of_month - timedelta(days=1)
            year = last_month.year
            month = last_month.month

        month_str = f"{year}-{month:02d}"
        
        self.stdout.write(f'Finalizing salaries for {month_str}...')

        # Get instructors to finalize
        if options['instructor_id']:
            instructors = Instructor.objects.filter(id=options['instructor_id'], is_active=True)
            if not instructors.exists():
                self.stdout.write(self.style.ERROR(f'Instructor {options["instructor_id"]} not found'))
                return
        else:
            instructors = Instructor.objects.filter(is_active=True)

        finalized_count = 0
        skipped_count = 0
        error_count = 0

        for instructor in instructors:
            try:
                # Check if already finalized
                existing = InstructorMonthlySnapshot.objects.filter(
                    instructor=instructor,
                    month=month_str,
                    is_finalized=True
                ).first()

                if existing:
                    self.stdout.write(
                        self.style.WARNING(
                            f'  Skipped {instructor.full_name}: Already finalized'
                        )
                    )
                    skipped_count += 1
                    continue

                # Calculate finalized month totals using tier/override logic.
                # For a past month, effective_end is the month end (no cap).
                month_str = f"{year}-{month:02d}"
                total_salary, occurrences, lesson_templates = calculate_instructor_salary_for_month(
                    instructor,
                    month_str,
                    effective_end=None,
                )
                total_revenue, total_students, revenue_occ = calculate_instructor_revenue_for_month(
                    instructor,
                    month_str,
                    effective_end=None,
                )
                profit = total_revenue - total_salary

                # payment_per_lesson is not constant when tiers/overrides exist.
                # Store an average for reference.
                payment_per_lesson = (total_salary / Decimal(occurrences)) if occurrences else Decimal('0.00')

                # Create or update snapshot
                snapshot, created = InstructorMonthlySnapshot.objects.update_or_create(
                    instructor=instructor,
                    month=month_str,
                    defaults={
                        'lesson_count': lesson_templates,  # Number of lesson templates
                        'payment_per_lesson': payment_per_lesson,
                        'total_salary': total_salary,
                        'total_students': total_students,
                        'total_revenue': total_revenue,
                        'profit': profit,
                        'is_finalized': True,
                        'total_lessons': lesson_templates,  # Same as lesson_count
                    }
                )

                action = 'Created' if created else 'Updated'
                self.stdout.write(
                    self.style.SUCCESS(
                        f'  {action} {instructor.full_name}: {lesson_count} lessons, ₪{total_salary}'
                    )
                )
                finalized_count += 1

            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f'  Error for {instructor.full_name}: {str(e)}'
                    )
                )
                error_count += 1

        # Summary
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'Finalized: {finalized_count}'))
        if skipped_count > 0:
            self.stdout.write(self.style.WARNING(f'Skipped: {skipped_count}'))
        if error_count > 0:
            self.stdout.write(self.style.ERROR(f'Errors: {error_count}'))
        
        self.stdout.write(self.style.SUCCESS(f'\nCompleted salary finalization for {month_str}'))

