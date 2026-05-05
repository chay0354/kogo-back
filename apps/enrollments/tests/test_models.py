"""
Unit tests for Enrollments app models.

Tests coverage:
- Enrollment: course enrollment (deprecated)
- LessonEnrollment: lesson enrollment with status
- LessonAttendance: attendance tracking
- ChildAbsence: absence tracking for analysis
"""
from datetime import date, timedelta
from django.test import TestCase

from apps.core.tests.test_fixtures import TestDataFactory
from apps.enrollments.models import Enrollment, LessonEnrollment, LessonAttendance, ChildAbsence


class EnrollmentModelTest(TestCase):
    """Test Enrollment model (deprecated)"""
    
    def setUp(self):
        self.course = TestDataFactory.create_course()
        self.child = TestDataFactory.create_child()
    
    def test_create_enrollment(self):
        """Test creating an enrollment"""
        enrollment = Enrollment.objects.create(
            course=self.course,
            child=self.child,
            is_active=True
        )
        
        self.assertEqual(enrollment.course, self.course)
        self.assertEqual(enrollment.child, self.child)
        self.assertTrue(enrollment.is_active)
    
    def test_enrollment_unique_constraint(self):
        """Test enrollment has unique constraint on course+child"""
        Enrollment.objects.create(
            course=self.course,
            child=self.child
        )
        
        # Creating duplicate should raise error
        with self.assertRaises(Exception):  # IntegrityError
            Enrollment.objects.create(
                course=self.course,
                child=self.child
            )
    
    def test_enrollment_str_representation(self):
        """Test enrollment string representation"""
        enrollment = Enrollment.objects.create(
            course=self.course,
            child=self.child
        )
        
        str_repr = str(enrollment)
        self.assertIn(self.child.full_name, str_repr)
        self.assertIn(self.course.name, str_repr)
    
    def test_enrollment_cascade_delete_with_course(self):
        """Test enrollment is deleted when course is deleted"""
        enrollment = Enrollment.objects.create(
            course=self.course,
            child=self.child
        )
        
        enrollment_id = enrollment.id
        self.course.delete()
        
        with self.assertRaises(Enrollment.DoesNotExist):
            Enrollment.objects.get(id=enrollment_id)
    
    def test_enrollment_cascade_delete_with_child(self):
        """Test enrollment is deleted when child is deleted"""
        enrollment = Enrollment.objects.create(
            course=self.course,
            child=self.child
        )
        
        enrollment_id = enrollment.id
        self.child.delete()
        
        with self.assertRaises(Enrollment.DoesNotExist):
            Enrollment.objects.get(id=enrollment_id)


class LessonEnrollmentModelTest(TestCase):
    """Test LessonEnrollment model"""
    
    def setUp(self):
        self.lesson = TestDataFactory.create_lesson()
        self.child = TestDataFactory.create_child()
    
    def test_create_lesson_enrollment(self):
        """Test creating a lesson enrollment"""
        enrollment = LessonEnrollment.objects.create(
            lesson=self.lesson,
            child=self.child,
            status='active'
        )
        
        self.assertEqual(enrollment.lesson, self.lesson)
        self.assertEqual(enrollment.child, self.child)
        self.assertEqual(enrollment.status, 'active')
    
    def test_lesson_enrollment_status_choices(self):
        """Test lesson enrollment status choices"""
        statuses = ['active', 'inactive', 'payments_problem']
        
        for status in statuses:
            child = TestDataFactory.create_child(first_name=f"Child-{status}")
            enrollment = LessonEnrollment.objects.create(
                lesson=self.lesson,
                child=child,
                status=status
            )
            self.assertEqual(enrollment.status, status)
    
    def test_lesson_enrollment_with_date_range(self):
        """Test lesson enrollment with start and end dates"""
        start_date = date.today()
        end_date = date.today() + timedelta(days=90)
        
        enrollment = LessonEnrollment.objects.create(
            lesson=self.lesson,
            child=self.child,
            status='active',
            start_date=start_date,
            end_date=end_date
        )
        
        self.assertEqual(enrollment.start_date, start_date)
        self.assertEqual(enrollment.end_date, end_date)
    
    def test_lesson_enrollment_with_notes(self):
        """Test lesson enrollment with notes"""
        enrollment = LessonEnrollment.objects.create(
            lesson=self.lesson,
            child=self.child,
            status='active',
            notes="ילד חדש, צריך תשומת לב מיוחדת"
        )
        
        self.assertEqual(enrollment.notes, "ילד חדש, צריך תשומת לב מיוחדת")
    
    def test_lesson_enrollment_unique_constraint(self):
        """Test lesson enrollment has unique constraint on lesson+child"""
        LessonEnrollment.objects.create(
            lesson=self.lesson,
            child=self.child
        )
        
        # Creating duplicate should raise error
        with self.assertRaises(Exception):  # IntegrityError
            LessonEnrollment.objects.create(
                lesson=self.lesson,
                child=self.child
            )
    
    def test_lesson_enrollment_str_representation(self):
        """Test lesson enrollment string representation"""
        enrollment = LessonEnrollment.objects.create(
            lesson=self.lesson,
            child=self.child
        )
        
        str_repr = str(enrollment)
        self.assertIn(self.child.full_name, str_repr)
    
    def test_lesson_enrollment_cascade_delete_with_lesson(self):
        """Test lesson enrollment is deleted when lesson is deleted"""
        enrollment = LessonEnrollment.objects.create(
            lesson=self.lesson,
            child=self.child
        )
        
        enrollment_id = enrollment.id
        self.lesson.delete()
        
        with self.assertRaises(LessonEnrollment.DoesNotExist):
            LessonEnrollment.objects.get(id=enrollment_id)
    
    def test_lesson_enrollment_cascade_delete_with_child(self):
        """Test lesson enrollment is deleted when child is deleted"""
        enrollment = LessonEnrollment.objects.create(
            lesson=self.lesson,
            child=self.child
        )
        
        enrollment_id = enrollment.id
        self.child.delete()
        
        with self.assertRaises(LessonEnrollment.DoesNotExist):
            LessonEnrollment.objects.get(id=enrollment_id)


class LessonAttendanceModelTest(TestCase):
    """Test LessonAttendance model"""
    
    def setUp(self):
        self.lesson = TestDataFactory.create_lesson()
        self.child = TestDataFactory.create_child()
    
    def test_create_lesson_attendance(self):
        """Test creating a lesson attendance record"""
        occurrence_date = date.today()
        
        attendance = LessonAttendance.objects.create(
            lesson=self.lesson,
            child=self.child,
            occurrence_date=occurrence_date,
            status='present'
        )
        
        self.assertEqual(attendance.lesson, self.lesson)
        self.assertEqual(attendance.child, self.child)
        self.assertEqual(attendance.occurrence_date, occurrence_date)
        self.assertEqual(attendance.status, 'present')
    
    def test_lesson_attendance_status_choices(self):
        """Test lesson attendance status choices"""
        statuses = ['present', 'absent', 'not_marked']
        
        for idx, status in enumerate(statuses):
            attendance = LessonAttendance.objects.create(
                lesson=self.lesson,
                child=self.child,
                occurrence_date=date.today() - timedelta(days=idx),
                status=status
            )
            self.assertEqual(attendance.status, status)
    
    def test_lesson_attendance_with_notes(self):
        """Test lesson attendance with notes"""
        attendance = LessonAttendance.objects.create(
            lesson=self.lesson,
            child=self.child,
            occurrence_date=date.today(),
            status='absent',
            notes="הורה התקשר מראש"
        )
        
        self.assertEqual(attendance.notes, "הורה התקשר מראש")
    
    def test_lesson_attendance_unique_constraint(self):
        """Test lesson attendance has unique constraint on lesson+child+occurrence_date"""
        occurrence_date = date.today()
        
        LessonAttendance.objects.create(
            lesson=self.lesson,
            child=self.child,
            occurrence_date=occurrence_date,
            status='present'
        )
        
        # Creating duplicate should raise error
        with self.assertRaises(Exception):  # IntegrityError
            LessonAttendance.objects.create(
                lesson=self.lesson,
                child=self.child,
                occurrence_date=occurrence_date,
                status='absent'
            )
    
    def test_lesson_attendance_str_representation(self):
        """Test lesson attendance string representation"""
        attendance = LessonAttendance.objects.create(
            lesson=self.lesson,
            child=self.child,
            occurrence_date=date.today(),
            status='present'
        )
        
        str_repr = str(attendance)
        self.assertIn(self.child.full_name, str_repr)
        self.assertIn('נוכח', str_repr)  # Display name for 'present'
    
    def test_lesson_attendance_multiple_dates(self):
        """Test child can have multiple attendance records for different dates"""
        date1 = date.today() - timedelta(days=7)
        date2 = date.today() - timedelta(days=14)
        date3 = date.today() - timedelta(days=21)
        
        attendance1 = LessonAttendance.objects.create(
            lesson=self.lesson,
            child=self.child,
            occurrence_date=date1,
            status='present'
        )
        
        attendance2 = LessonAttendance.objects.create(
            lesson=self.lesson,
            child=self.child,
            occurrence_date=date2,
            status='absent'
        )
        
        attendance3 = LessonAttendance.objects.create(
            lesson=self.lesson,
            child=self.child,
            occurrence_date=date3,
            status='present'
        )
        
        attendance_records = self.child.attendance_records.all()
        self.assertEqual(attendance_records.count(), 3)


class ChildAbsenceModelTest(TestCase):
    """Test ChildAbsence model"""
    
    def setUp(self):
        self.course = TestDataFactory.create_course()
        self.lesson = TestDataFactory.create_lesson(course=self.course)
        self.child = TestDataFactory.create_child()
    
    def test_create_child_absence(self):
        """Test creating a child absence record"""
        occurrence_date = date.today() - timedelta(days=7)
        
        absence = ChildAbsence.objects.create(
            child=self.child,
            lesson=self.lesson,
            course=self.course,
            occurrence_date=occurrence_date
        )
        
        self.assertEqual(absence.child, self.child)
        self.assertEqual(absence.lesson, self.lesson)
        self.assertEqual(absence.course, self.course)
        self.assertEqual(absence.occurrence_date, occurrence_date)
    
    def test_child_absence_unique_constraint(self):
        """Test child absence has unique constraint on child+lesson+occurrence_date"""
        occurrence_date = date.today()
        
        ChildAbsence.objects.create(
            child=self.child,
            lesson=self.lesson,
            course=self.course,
            occurrence_date=occurrence_date
        )
        
        # Creating duplicate should raise error
        with self.assertRaises(Exception):  # IntegrityError
            ChildAbsence.objects.create(
                child=self.child,
                lesson=self.lesson,
                course=self.course,
                occurrence_date=occurrence_date
            )
    
    def test_child_absence_str_representation(self):
        """Test child absence string representation"""
        absence = ChildAbsence.objects.create(
            child=self.child,
            lesson=self.lesson,
            course=self.course,
            occurrence_date=date.today()
        )
        
        str_repr = str(absence)
        self.assertIn(self.child.full_name, str_repr)
    
    def test_child_multiple_absences(self):
        """Test child can have multiple absence records"""
        absence1 = ChildAbsence.objects.create(
            child=self.child,
            lesson=self.lesson,
            course=self.course,
            occurrence_date=date.today() - timedelta(days=7)
        )
        
        absence2 = ChildAbsence.objects.create(
            child=self.child,
            lesson=self.lesson,
            course=self.course,
            occurrence_date=date.today() - timedelta(days=14)
        )
        
        absence3 = ChildAbsence.objects.create(
            child=self.child,
            lesson=self.lesson,
            course=self.course,
            occurrence_date=date.today() - timedelta(days=21)
        )
        
        absences = self.child.absences.all()
        self.assertEqual(absences.count(), 3)
    
    def test_child_absence_ordering(self):
        """Test child absences are ordered by -occurrence_date (most recent first)"""
        date1 = date.today() - timedelta(days=30)
        date2 = date.today() - timedelta(days=20)
        date3 = date.today() - timedelta(days=10)
        
        absence1 = ChildAbsence.objects.create(
            child=self.child,
            lesson=self.lesson,
            course=self.course,
            occurrence_date=date1
        )
        
        absence2 = ChildAbsence.objects.create(
            child=self.child,
            lesson=self.lesson,
            course=self.course,
            occurrence_date=date2
        )
        
        absence3 = ChildAbsence.objects.create(
            child=self.child,
            lesson=self.lesson,
            course=self.course,
            occurrence_date=date3
        )
        
        absences = list(ChildAbsence.objects.filter(child=self.child))
        # Most recent first
        self.assertEqual(absences[0], absence3)
        self.assertEqual(absences[1], absence2)
        self.assertEqual(absences[2], absence1)
    
    def test_child_absence_cascade_delete_with_child(self):
        """Test child absence is deleted when child is deleted"""
        absence = ChildAbsence.objects.create(
            child=self.child,
            lesson=self.lesson,
            course=self.course,
            occurrence_date=date.today()
        )
        
        absence_id = absence.id
        self.child.delete()
        
        with self.assertRaises(ChildAbsence.DoesNotExist):
            ChildAbsence.objects.get(id=absence_id)
