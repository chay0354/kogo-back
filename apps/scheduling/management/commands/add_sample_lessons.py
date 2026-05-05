from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import datetime, time, timedelta
from apps.courses.models import Course, CourseType, Lesson
from apps.instructors.models import Instructor
from apps.core.models import Branch, Room


class Command(BaseCommand):
    help = 'Add sample lessons to the schedule for testing'

    def handle(self, *args, **options):
        self.stdout.write('Adding sample lessons...')

        # Get or create test data
        branch = Branch.objects.first()
        if not branch:
            self.stdout.write(self.style.ERROR('No branch found. Please create a branch first.'))
            return

        instructor = Instructor.objects.first()
        if not instructor:
            self.stdout.write(self.style.ERROR('No instructor found. Please create an instructor first.'))
            return

        course_type = CourseType.objects.first()
        if not course_type:
            course_type = CourseType.objects.create(
                name='קפואירה',
                description='אומנויות לחימה ברזילאיות'
            )
            self.stdout.write(self.style.SUCCESS(f'Created course type: {course_type.name}'))

        course = Course.objects.first()
        if not course:
            course = Course.objects.create(
                course_type=course_type,
                name='י-ב',
                description='קבוצה לגילאי 10-12',
                price=300,
                capacity=20,
                branch=branch,
                min_age=10,
                max_age=12
            )
            self.stdout.write(self.style.SUCCESS(f'Created course: {course.name}'))

        room = Room.objects.filter(branch=branch).first()

        # Get current week dates
        today = timezone.now().date()
        
        # Find the start of current week (Sunday)
        days_since_sunday = (today.weekday() + 1) % 7
        week_start = today - timedelta(days=days_since_sunday)

        # Sample lessons for the week
        lessons_data = [
            # Sunday (day 0)
            {'day': 0, 'start': '09:00', 'end': '10:00', 'offset': 0},
            {'day': 0, 'start': '14:00', 'end': '15:00', 'offset': 0},
            
            # Monday (day 1)
            {'day': 1, 'start': '16:00', 'end': '17:00', 'offset': 1},
            
            # Tuesday (day 2)
            {'day': 2, 'start': '10:00', 'end': '11:00', 'offset': 2},
            {'day': 2, 'start': '17:00', 'end': '18:00', 'offset': 2},
            
            # Wednesday (day 3)
            {'day': 3, 'start': '14:00', 'end': '15:00', 'offset': 3},
            {'day': 3, 'start': '15:00', 'end': '16:00', 'offset': 3},
            
            # Thursday (day 4)
            {'day': 4, 'start': '09:00', 'end': '10:00', 'offset': 4},
            {'day': 4, 'start': '14:00', 'end': '15:00', 'offset': 4},
            
            # Friday (day 5)
            {'day': 5, 'start': '13:00', 'end': '14:00', 'offset': 5},
            {'day': 5, 'start': '12:00', 'end': '13:00', 'offset': 5},
        ]

        created_count = 0
        for lesson_data in lessons_data:
            lesson_date = week_start + timedelta(days=lesson_data['offset'])
            
            # Parse time
            start_parts = lesson_data['start'].split(':')
            start_time = time(int(start_parts[0]), int(start_parts[1]))
            
            end_parts = lesson_data['end'].split(':')
            end_time = time(int(end_parts[0]), int(end_parts[1]))

            # Check if lesson already exists
            existing = Lesson.objects.filter(
                course=course,
                day_of_week=lesson_data['day'],
                start_time=start_time,
                lesson_date=lesson_date
            ).first()

            if existing:
                self.stdout.write(f'  Lesson already exists: {existing}')
                continue

            # Create lesson
            lesson = Lesson.objects.create(
                course=course,
                branch=branch,
                room=room,
                instructor=instructor,
                day_of_week=lesson_data['day'],
                start_time=start_time,
                end_time=end_time,
                lesson_date=lesson_date,
                is_recurring=True,
                status='scheduled'
            )
            created_count += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f'  Created lesson: {lesson.get_day_of_week_display()} {lesson.start_time} - {lesson_date}'
                )
            )

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'Successfully created {created_count} sample lessons!'))
        self.stdout.write('')
        self.stdout.write('You can now view them at /schedule in the frontend.')

