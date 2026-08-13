from datetime import date, time

from django.core.management.base import BaseCommand

from apps.core.models import Branch, Room
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Child, Family
from apps.enrollments.enrollment_counts import count_paying_enrollments
from apps.enrollments.models import LessonEnrollment

DEMO_MARKER = '[WIDGET-DEMO-FULL]'


class Command(BaseCommand):
    help = 'Create a demo course with one full lesson and one open lesson for widget testing.'

    def handle(self, *args, **options):
        branch = Branch.objects.filter(is_active=True, name__icontains='דמרי').first()
        if not branch:
            branch = Branch.objects.filter(is_active=True).first()
        if not branch:
            self.stderr.write('No active branch found')
            return

        course_type = (
            CourseType.objects.filter(is_active=True, name__icontains='היפהופ').first()
            or CourseType.objects.filter(is_active=True).first()
        )
        if not course_type:
            self.stderr.write('No course type found')
            return

        room = Room.objects.filter(branch=branch, is_active=True).first()
        if not room:
            room = Room.objects.create(branch=branch, name='Demo room', capacity=2, is_active=True)

        course, _ = Course.objects.get_or_create(
            branch=branch,
            name=f'{DEMO_MARKER} hip hop test',
            defaults={
                'course_type': course_type,
                'price': 250,
                'capacity': 2,
                'min_age': 3,
                'max_age': 8,
                'is_active': True,
            },
        )
        course.capacity = 2
        course.is_active = True
        course.min_age = 3
        course.max_age = 8
        course.course_type = course_type
        course.save()

        full_lesson, _ = Lesson.objects.get_or_create(
            course=course,
            day_of_week=0,
            start_time=time(16, 0),
            defaults={'end_time': time(17, 0), 'room': room, 'is_recurring': True},
        )
        full_lesson.room = room
        full_lesson.end_time = time(17, 0)
        full_lesson.save()

        open_lesson, _ = Lesson.objects.get_or_create(
            course=course,
            day_of_week=2,
            start_time=time(17, 0),
            defaults={'end_time': time(18, 0), 'room': room, 'is_recurring': True},
        )
        open_lesson.room = room
        open_lesson.end_time = time(18, 0)
        open_lesson.save()

        while count_paying_enrollments(lesson=full_lesson) < course.capacity:
            n = count_paying_enrollments(lesson=full_lesson) + 1
            family = Family.objects.create(
                name=f'Demo Family {n}',
                phone=f'050000{n:04d}',
                email=f'demo{n}@example.com',
                parent_id_number=f'99999999{n}',
                branch=branch,
            )
            child = Child.objects.create(
                family=family,
                first_name=f'Demo{n}',
                last_name='Full',
                status='active',
                birth_date=date(2018, 1, 1),
                gender='male',
            )
            LessonEnrollment.objects.get_or_create(
                child=child,
                lesson=full_lesson,
                defaults={'start_date': date.today(), 'status': 'active'},
            )

        self.stdout.write(f'BRANCH_ID={branch.id}')
        self.stdout.write(f'BRANCH_NAME={branch.name}')
        self.stdout.write(f'COURSE_ID={course.id}')
        self.stdout.write(f'COURSE_NAME={course.name}')
        self.stdout.write(f'COURSE_TYPE_ID={course.course_type_id}')
        self.stdout.write(f'COURSE_TYPE_NAME={course.course_type.name}')
        self.stdout.write(f'FULL_LESSON_ID={full_lesson.id}')
        self.stdout.write(f'FULL_LESSON_DAY=0 (Sunday 16:00-17:00)')
        self.stdout.write(f'OPEN_LESSON_ID={open_lesson.id}')
        self.stdout.write(f'OPEN_LESSON_DAY=2 (Tuesday 17:00-18:00)')
        self.stdout.write(f'CAPACITY={course.capacity}')
        self.stdout.write(f'FULL_LESSON_ENROLLED={count_paying_enrollments(lesson=full_lesson)}')
        self.stdout.write(f'OPEN_LESSON_ENROLLED={count_paying_enrollments(lesson=open_lesson)}')
        self.stdout.write(f'AGES={course.min_age}-{course.max_age}')
