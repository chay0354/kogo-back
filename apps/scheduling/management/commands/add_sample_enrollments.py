from django.core.management.base import BaseCommand
from apps.courses.models import Lesson
from apps.enrollments.models import LessonEnrollment
from apps.customers.models import Child


class Command(BaseCommand):
    help = 'Add sample enrollments to lessons'

    def handle(self, *args, **options):
        self.stdout.write('Adding sample enrollments...')

        # Get lessons
        lessons = Lesson.objects.filter(status='scheduled').order_by('lesson_date', 'start_time')
        if not lessons.exists():
            self.stdout.write(self.style.ERROR('No lessons found. Please run add_sample_lessons first.'))
            return

        # Get children
        children = Child.objects.filter(status='active')[:10]
        if not children.exists():
            self.stdout.write(self.style.WARNING('No active children found in the database.'))
            self.stdout.write('Please add children through the admin or customers page.')
            return

        created_count = 0
        for lesson in lessons:
            # Add 2-5 random students to each lesson
            import random
            num_students = random.randint(2, min(5, children.count()))
            selected_children = random.sample(list(children), num_students)

            for child in selected_children:
                # Check if already enrolled
                existing = LessonEnrollment.objects.filter(
                    lesson=lesson,
                    child=child
                ).first()

                if existing:
                    continue

                # Create enrollment
                LessonEnrollment.objects.create(
                    lesson=lesson,
                    child=child,
                    status='active'
                )
                created_count += 1

            self.stdout.write(
                self.style.SUCCESS(
                    f'  Added {num_students} students to: {lesson.get_day_of_week_display()} {lesson.start_time}'
                )
            )

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'Successfully created {created_count} enrollments!'))

