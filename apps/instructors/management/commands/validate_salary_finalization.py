from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta

from apps.instructors.models import Instructor
from apps.core.models import InstructorMonthlySnapshot


class Command(BaseCommand):
    help = 'Validate that previous month salary snapshots are finalized for all active instructors'

    def handle(self, *args, **options):
        today = timezone.now().date()
        first_of_month = today.replace(day=1)
        prev_month_end = first_of_month - timedelta(days=1)
        prev_month = prev_month_end.strftime('%Y-%m')

        instructors = Instructor.objects.filter(is_active=True)
        missing = []

        for inst in instructors:
            ok = InstructorMonthlySnapshot.objects.filter(
                instructor=inst,
                month=prev_month,
                is_finalized=True,
            ).exists()
            if not ok:
                missing.append(inst)

        if missing:
            self.stdout.write(self.style.ERROR(f'Missing finalized snapshots for {prev_month}: {len(missing)} instructor(s)'))
            for inst in missing[:50]:
                self.stdout.write(f'  - {inst.full_name} ({inst.id})')
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS(f'All active instructors have finalized snapshots for {prev_month}.'))

