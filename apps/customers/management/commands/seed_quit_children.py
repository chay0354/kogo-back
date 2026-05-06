"""
Seed two fictive children with status history to populate אחוז נשירה on the dashboard.
Each child starts as 'active' and has a ChildStatusHistory record within the current month.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import date, timedelta
from apps.customers.models import Child, Family
from apps.customers.status_history_models import ChildStatusHistory
from apps.core.models import Branch


class Command(BaseCommand):
    help = 'Seed two fictive children with quit status history for dashboard testing'

    def handle(self, *args, **kwargs):
        branch = Branch.objects.first()
        if not branch:
            self.stdout.write(self.style.ERROR('No branches found. Create a branch first.'))
            return

        # Reuse or create a test family
        family, created = Family.objects.get_or_create(
            name='משפחת בדיקה - נשירה',
            defaults={
                'phone': '050-0000000',
                'branch': branch,
            }
        )
        if created:
            self.stdout.write(f'Created family: {family.name}')

        today = date.today()
        # Place the status change mid-way through the current month so it falls
        # inside the default date range (start of month → today).
        changed_mid_month = timezone.now().replace(day=max(1, today.day - 7))

        children_data = [
            {
                'first_name': 'דני',
                'last_name': 'נשר',
                'birth_date': date(2016, 4, 10),
                'gender': 'male',
                'new_status': 'ghost',
                'changed_at': changed_mid_month,
            },
            {
                'first_name': 'מיכל',
                'last_name': 'עזב',
                'birth_date': date(2018, 11, 3),
                'gender': 'female',
                'new_status': 'inactive',
                'changed_at': changed_mid_month + timedelta(days=2),
            },
        ]

        for data in children_data:
            child, created = Child.objects.get_or_create(
                first_name=data['first_name'],
                last_name=data['last_name'],
                family=family,
                defaults={
                    'birth_date': data['birth_date'],
                    'gender': data['gender'],
                    'status': data['new_status'],
                }
            )
            if not created:
                child.status = data['new_status']
                child.save(update_fields=['status', 'updated_at'])

            # Create the status-history record directly (bypasses the signal
            # which only fires on save, not retroactively).
            record, hist_created = ChildStatusHistory.objects.get_or_create(
                child=child,
                previous_status='active',
                new_status=data['new_status'],
                defaults={'changed_at': data['changed_at']}
            )

            action = 'Created' if hist_created else 'Already exists'
            self.stdout.write(
                self.style.SUCCESS(
                    f"{action}: {child.full_name} | active → {data['new_status']} "
                    f"@ {record.changed_at.date()}"
                )
            )

        self.stdout.write(self.style.SUCCESS('Done. Refresh the students dashboard to see אחוז נשירה.'))
