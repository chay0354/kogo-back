"""
Management command to populate database with sample data
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import date, time, timedelta
import random
from calendar import monthrange

from apps.core.models import City, Branch, Room
from apps.instructors.models import Instructor, InstructorSalaryTier
from apps.courses.models import CourseType, Course, Lesson
from apps.customers.models import Family, Parent, Child
from apps.enrollments.models import LessonEnrollment, LessonAttendance
from apps.customers.financial_models import Invoice


class Command(BaseCommand):
    help = 'Populate database with sample data'

    def add_arguments(self, parser):
        parser.add_argument('--children', type=int, default=21, help='Number of children to create')
        parser.add_argument('--branches', type=int, default=3, help='Number of branches to create')
        parser.add_argument('--extra-courses', type=int, default=0, help='How many additional courses to add (on top of defaults)')
        parser.add_argument('--lessons-per-course', type=int, default=2, help='Lesson instances per course')
        parser.add_argument('--clear', action='store_true', default=True, help='Clear existing data first (default true)')

    def handle(self, *args, **kwargs):
        children_target = int(kwargs.get('children') or 21)
        branches_target = int(kwargs.get('branches') or 3)
        extra_courses = int(kwargs.get('extra_courses') or 0)
        lessons_per_course = int(kwargs.get('lessons_per_course') or 2)
        do_clear = bool(kwargs.get('clear'))

        self.stdout.write('Creating sample data...')
        
        # Clear existing data (optional)
        if do_clear:
            self.stdout.write('Clearing existing data...')
            LessonAttendance.objects.all().delete()
            LessonEnrollment.objects.all().delete()
            Lesson.objects.all().delete()
            Course.objects.all().delete()
            CourseType.objects.all().delete()
            Child.objects.all().delete()
            Parent.objects.all().delete()
            Family.objects.all().delete()
            InstructorSalaryTier.objects.all().delete()
            Instructor.objects.all().delete()
            Room.objects.all().delete()
            Branch.objects.all().delete()
            City.objects.all().delete()
        
        # Create cities
        self.stdout.write('Creating cities...')
        cities = []
        city_names = ['תל אביב', 'ירושלים', 'חיפה', 'באר שבע', 'ראשון לציון']
        for name in city_names:
            city = City.objects.create(name=name)
            cities.append(city)
        
        # Create branches
        self.stdout.write('Creating branches...')
        branches = []
        managers = ['דוד כהן', 'רחל לוי', 'משה אבני', 'שרה ישראלי', 'יוסי מזרחי', 'נועה שמש', 'איתי לוי', 'טל כהן']
        branches_to_create = []
        for i in range(branches_target):
            city = random.choice(cities)
            name = f'קוגומלו {city.name} #{i+1}' if branches_target > 3 else f'קוגומלו {city.name}'
            branches_to_create.append(Branch(
                name=name,
                city=city,
                address=f'רחוב {random.choice(["הרצל","דיזנגוף","יפו","הציונות","בן גוריון"])} {random.randint(1, 200)}',
                phone=f'0{random.randint(2,9)}-{random.randint(1000000,9999999)}',
                email=f'branch{i+1}@kogomalo.com',
                manager_name=random.choice(managers),
                is_active=True,
            ))
        Branch.objects.bulk_create(branches_to_create)
        branches = list(Branch.objects.all())

        # Rooms (3 per branch)
        rooms_to_create = []
        for b in branches:
            for r in range(1, 4):
                rooms_to_create.append(Room(
                    branch=b,
                    name=f'חדר {r}',
                    capacity=15 + (r * 5),
                    purpose='חוג קבוצתי',
                    is_active=True,
                ))
        Room.objects.bulk_create(rooms_to_create)
        
        # Create instructors
        self.stdout.write('Creating instructors...')
        instructors = []
        instructor_data = [
            ('דוד', 'כהן', '0501234567', 'david@kogomalo.com', 'קראטה'),
            ('רחל', 'לוי', '0502345678', 'rachel@kogomalo.com', 'בלט'),
            ('משה', 'אבני', '0503456789', 'moshe@kogomalo.com', 'כדורסל'),
            ('שרה', 'ישראלי', '0504567890', 'sarah@kogomalo.com', 'ציור'),
            ('יוסי', 'מזרחי', '0505678901', 'yossi@kogomalo.com', 'גיטרה'),
            ('נועה', 'שמש', '0506789012', 'noa@kogomalo.com', 'יוגה'),
        ]
        
        for idx, (first, last, phone, email, spec) in enumerate(instructor_data):
            # Alternate between fixed and tiered salary models
            salary_model = 'tiered_by_students' if idx % 2 == 0 else 'fixed_per_lesson'
            
            instructor = Instructor.objects.create(
                first_name=first,
                last_name=last,
                phone=phone,
                email=email,
                specialization=spec,
                primary_branch=random.choice(branches),
                salary_model_type=salary_model,
                fixed_salary_per_lesson=250,
                is_active=True
            )
            
            # Add salary tiers for tiered instructors
            if salary_model == 'tiered_by_students':
                InstructorSalaryTier.objects.bulk_create([
                    InstructorSalaryTier(instructor=instructor, min_students=0, max_students=5, salary_per_lesson=150),
                    InstructorSalaryTier(instructor=instructor, min_students=6, max_students=10, salary_per_lesson=200),
                    InstructorSalaryTier(instructor=instructor, min_students=11, max_students=None, salary_per_lesson=250),
                ])
            
            instructors.append(instructor)
        
        # Create course types
        self.stdout.write('Creating course types...')
        course_type_data = [
            ('קפוארה', 'אמנות לחימה ברזילאית המשלבת ריקוד, אקרובטיקה ומוזיקה'),
            ('כדורסל', 'משחק קבוצתי המפתח כושר גופני ועבודת צוות'),
            ('בריקדנס', 'ריקוד אורבני אנרגטי ומודרני'),
            ('ג\'ודו', 'אמנות לחימה יפנית המלמדת משמעת ותרבות'),
            ('ציור', 'פיתוח יצירתיות וכישורי אמנות חזותית'),
            ('גיטרה', 'לימוד נגינה במגוון סגנונות מוזיקליים'),
        ]
        
        course_types = []
        for name, description in course_type_data:
            course_type = CourseType.objects.create(
                name=name,
                description=description,
                is_active=True
            )
            course_types.append(course_type)
        
        # Create courses
        self.stdout.write('Creating courses...')
        courses = []
        course_data = [
            (course_types[0], 'מתחילים כיתות א\'-ג\'', 'מתחילים', 350, 15, 6, 8),
            (course_types[0], 'מתקדמים כיתות ד\'-ו\'', 'מתקדמים', 400, 15, 9, 11),
            (course_types[1], 'נבחרת נערים', 'אימוני נבחרת', 300, 20, 10, 14),
            (course_types[1], 'מתחילים צעירים', 'יסודות כדורסל', 280, 18, 6, 9),
            (course_types[2], 'בריקדנס התחלתי', 'יסודות ריקוד אורבני', 320, 12, 7, 10),
            (course_types[3], 'ג\'ודו לילדים', 'משמעת ואומנות לחימה', 380, 15, 6, 12),
            (course_types[4], 'ציור ויצירה', 'אמנות לכל הגילאים', 280, 10, 5, 12),
            (course_types[5], 'גיטרה פרטי', 'שיעורים אישיים', 500, 1, 8, 15),
        ]

        # Add extra courses if requested
        extra_names = ['כדורגל', 'התעמלות', 'תיאטרון', 'מדעים', 'קודינג', 'שחייה', 'פילאטיס', 'נגינה', 'קרב מגע', 'רובוטיקה']
        for i in range(extra_courses):
            base = random.choice(extra_names)
            ct = random.choice(course_types)
            course_data.append((
                ct,
                f'{base} #{i+1}',
                f'חוג {base} מתקדם',
                random.choice([220, 260, 300, 350, 420]),
                random.choice([10, 12, 15, 18, 20]),
                random.choice([4, 5, 6, 7, 8, 10]),
                random.choice([9, 10, 12, 14, 16, 18]),
            ))
        
        courses_to_create = []
        for course_type, name, desc, price, capacity, min_age, max_age in course_data:
            courses_to_create.append(Course(
                course_type=course_type,
                name=name,
                description=desc,
                price=price,
                capacity=capacity,
                branch=None,  # Courses are not tied to specific branches
                min_age=min_age,
                max_age=max_age,
                is_active=True,
            ))
        Course.objects.bulk_create(courses_to_create)
        courses = list(Course.objects.all())
        
        # Create lessons
        self.stdout.write('Creating lessons...')
        lessons = []
        lessons_to_create = []
        rooms_list = list(Room.objects.all())
        for course in courses:
            for _ in range(lessons_per_course):
                day = random.randint(0, 5)  # Sunday to Friday
                hour = random.choice([14, 15, 16, 17, 18])
                branch = random.choice(branches)
                room = random.choice([r for r in rooms_list if r.branch == branch] + [None])
                lessons_to_create.append(Lesson(
                    course=course,
                    branch=branch,
                    room=room,
                    instructor=random.choice(instructors),
                    day_of_week=day,
                    start_time=time(hour, 0),
                    end_time=time(hour + 1, 0),
                    is_recurring=True,
                    status='scheduled',
                ))
        Lesson.objects.bulk_create(lessons_to_create)
        lessons = list(Lesson.objects.all())
        
        # Create families and children (bulk)
        self.stdout.write('Creating families and children...')
        children_first_names_male = ['נועם', 'איתי', 'יובל', 'אורי', 'תומר', 'רועי', 'עומר', 'גיא', 'אלון', 'אדם']
        children_first_names_female = ['נועה', 'מיכל', 'שירה', 'תמר', 'יעל', 'רותם', 'אלה', 'מאיה', 'דנה', 'ליה']
        last_names = ['כהן', 'לוי', 'מזרחי', 'פרידמן', 'אשתור', 'אלמוג', 'דהן', 'בכר', 'וינר', 'זוהר', 'אשכנזי', 'אדרי']
        parent_first_names = ['אורי', 'מיכל', 'יונתן', 'ליאת', 'טל', 'רון', 'שירה', 'דנה', 'עמר', 'יעקב', 'גלית']

        # we’ll create ~ children_target/2 families (avg 2 kids per family)
        families_target = max(1, children_target // 2)
        families_to_create = []
        for i in range(families_target):
            ln = random.choice(last_names)
            pf = random.choice(parent_first_names)
            b = random.choice(branches)
            phone = f'05{random.randint(0,9)}{random.randint(1000000,9999999)}'
            families_to_create.append(Family(
                name=f'משפחת {ln}',
                phone=phone,
                email=f'family{i+1}@example.com',
                address=f'רחוב {random.choice(["הרצל","דיזנגוף","יפו","הציונות","בן גוריון"])} {random.randint(1, 200)}, {b.city.name}',
                branch=b,
                notes='',
            ))
        Family.objects.bulk_create(families_to_create)
        families = list(Family.objects.all())

        parents_to_create = []
        for fam in families:
            # single primary parent per family (for speed)
            parts = fam.name.split(' ')
            ln = parts[-1] if parts else 'כהן'
            pf = random.choice(parent_first_names)
            parents_to_create.append(Parent(
                family=fam,
                first_name=pf,
                last_name=ln,
                phone=fam.phone,
                email=fam.email,
                is_primary=True,
            ))
        Parent.objects.bulk_create(parents_to_create)

        # Children creation
        all_children = []
        children_to_create = []
        today = date.today()
        last_day = monthrange(today.year, today.month)[1]
        paid_this_month_until = date(today.year, today.month, last_day)

        for i in range(children_target):
            fam = random.choice(families)
            ln = fam.name.split(' ')[-1]
            gender = random.choice(['male', 'female'])
            fn = random.choice(children_first_names_male if gender == 'male' else children_first_names_female)

            # age 4-14
            age = random.randint(4, 14)
            birth_date = today - timedelta(days=age * 365)

            # status distribution for large test (mostly active)
            child_status = random.choices(
                ['active', 'payment_problem', 'trial', 'expired'],
                weights=[70, 15, 10, 5],
            )[0]

            if child_status == 'trial':
                subscription_start = None
                subscription_end = None
                paid_until = None
                trial_count = random.randint(1, 3)
            elif child_status == 'expired':
                subscription_start = date(2024, 9, 1)
                subscription_end = date(2025, 7, 1)
                paid_until = date(2025, 1, 31)
                trial_count = 0
            elif child_status == 'payment_problem':
                subscription_start = date(2024, 9, 1)
                subscription_end = date(2025, 7, 1)
                paid_until = date(today.year, max(1, today.month - 1), 28)
                trial_count = 0
            else:
                subscription_start = date(2024, 9, 1)
                subscription_end = date(2025, 7, 1)
                paid_until = paid_this_month_until
                trial_count = 0

            children_to_create.append(Child(
                family=fam,
                first_name=fn,
                last_name=ln,
                birth_date=birth_date,
                gender=gender,
                status=child_status,
                paid_until_date=paid_until,
                trial_classes_attended=trial_count,
                subscription_start_date=subscription_start,
                subscription_end_date=subscription_end,
                notes='',
            ))

        Child.objects.bulk_create(children_to_create)
        all_children = list(Child.objects.all())
        
        # Create enrollments
        self.stdout.write('Creating enrollments...')
        enrollments_to_create = []
        attendance_to_create = []
        now = timezone.now()
        for child in all_children:
            if child.status in ['active', 'payment_problem']:
                lesson = random.choice(lessons)
                enrollments_to_create.append(LessonEnrollment(
                    lesson=lesson,
                    child=child,
                    status='active',
                    start_date=child.subscription_start_date,
                    enrolled_at=now - timedelta(days=random.randint(10, 90)),
                    notes='',
                ))
                attendance_status = random.choices(
                    ['present', 'absent', 'not_marked'],
                    weights=[70, 20, 10],
                )[0]
                attendance_to_create.append(LessonAttendance(
                    lesson=lesson,
                    child=child,
                    status=attendance_status,
                    notes='',
                ))
        LessonEnrollment.objects.bulk_create(enrollments_to_create)
        LessonAttendance.objects.bulk_create(attendance_to_create)
        
        # Create some invoices for payment status
        self.stdout.write('Creating invoices...')
        current_month = timezone.now().month
        current_year = timezone.now().year
        
        for family in families[:7]:  # 7 out of 10 families paid this month
            Invoice.objects.create(
                invoice_number=f'INV-{random.randint(1000, 9999)}',
                family=family,
                branch=family.branch,
                amount=random.randint(300, 1200),
                status='paid',
                payment_method='credit_card',
                invoice_date=timezone.datetime(current_year, current_month, random.randint(1, 28)),
                payer_name=family.name
            )
        
        self.stdout.write(self.style.SUCCESS('✓ Successfully populated database!'))
        self.stdout.write(f'Created:')
        self.stdout.write(f'  - {City.objects.count()} cities')
        self.stdout.write(f'  - {Branch.objects.count()} branches')
        self.stdout.write(f'  - {Room.objects.count()} rooms')
        self.stdout.write(f'  - {Instructor.objects.count()} instructors')
        self.stdout.write(f'  - {InstructorSalaryTier.objects.count()} salary tiers')
        self.stdout.write(f'  - {CourseType.objects.count()} course types')
        self.stdout.write(f'  - {Course.objects.count()} courses')
        self.stdout.write(f'  - {Lesson.objects.count()} lessons')
        self.stdout.write(f'  - {Family.objects.count()} families')
        self.stdout.write(f'  - {Parent.objects.count()} parents')
        self.stdout.write(f'  - {Child.objects.count()} children')
        self.stdout.write(f'  - {LessonEnrollment.objects.count()} enrollments')
        self.stdout.write(f'  - {LessonAttendance.objects.count()} attendance records')
        self.stdout.write(f'  - {Invoice.objects.count()} invoices')

