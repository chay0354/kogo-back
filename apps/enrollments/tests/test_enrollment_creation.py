"""
Tests for Enrollment Creation Logic

Tests the duplicate prevention and reactivation logic in enrollment creation.
"""
from django.test import TestCase
from django.db import IntegrityError
from apps.enrollments.models import Enrollment
from apps.customers.tests.test_fixtures import (
    create_test_child,
    create_test_family,
    create_test_course,
    create_test_branch
)


class EnrollmentCreationTests(TestCase):
    """Test enrollment creation, duplicate prevention, and reactivation"""
    
    def setUp(self):
        """Set up test data"""
        self.branch = create_test_branch()
        self.family = create_test_family(branch=self.branch)
        self.child = create_test_child(family=self.family)
        self.course = create_test_course(branch=self.branch)
    
    def test_create_new_enrollment(self):
        """
        Test: Create a new enrollment successfully
        
        Scenario:
        - Child has never been enrolled in this course
        - Create new enrollment
        
        Expected:
        - Enrollment created successfully
        - is_active = True
        - Child and course linked correctly
        """
        enrollment = Enrollment.objects.create(
            course=self.course,
            child=self.child,
            is_active=True
        )
        
        # Verify enrollment was created
        self.assertIsNotNone(enrollment.id)
        self.assertTrue(enrollment.is_active)
        self.assertEqual(enrollment.course, self.course)
        self.assertEqual(enrollment.child, self.child)
        
        # Verify it's in the database
        self.assertEqual(Enrollment.objects.count(), 1)
        saved_enrollment = Enrollment.objects.first()
        self.assertEqual(saved_enrollment.child, self.child)
        self.assertEqual(saved_enrollment.course, self.course)
    
    def test_prevent_duplicate_active_enrollment(self):
        """
        Test: Cannot create duplicate active enrollment
        
        Scenario:
        - Child already enrolled in course (is_active=True)
        - Attempt to create second enrollment for same child+course
        
        Expected:
        - Database raises IntegrityError
        - unique_together constraint enforced
        """
        from django.db import transaction
        
        # Create first enrollment
        Enrollment.objects.create(
            course=self.course,
            child=self.child,
            is_active=True
        )
        
        # Attempt to create duplicate (must be in atomic block)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Enrollment.objects.create(
                    course=self.course,
                    child=self.child,
                    is_active=True
                )
        
        # Verify only one enrollment exists
        self.assertEqual(Enrollment.objects.count(), 1)
    
    def test_reactivate_inactive_enrollment(self):
        """
        Test: Reactivate an inactive enrollment
        
        Scenario:
        - Child was previously enrolled (is_active=False)
        - Re-enroll the child
        
        Expected:
        - Existing enrollment should be reactivated
        - is_active changes from False to True
        - No duplicate created
        """
        # Create inactive enrollment
        enrollment = Enrollment.objects.create(
            course=self.course,
            child=self.child,
            is_active=False
        )
        
        original_id = enrollment.id
        
        # Simulate reactivation (as done in EnrollmentViewSet.create)
        enrollment.is_active = True
        enrollment.save()
        
        # Verify it was reactivated, not duplicated
        self.assertEqual(Enrollment.objects.count(), 1)
        enrollment.refresh_from_db()
        self.assertTrue(enrollment.is_active)
        self.assertEqual(enrollment.id, original_id)
    
    def test_enrollment_unique_constraint(self):
        """
        Test: Database enforces unique_together constraint
        
        Scenario:
        - unique_together = ['course', 'child'] in model
        - Attempt to create duplicate even with different is_active values
        
        Expected:
        - Cannot have two enrollments for same course+child
        """
        from django.db import transaction
        
        # Create first enrollment
        Enrollment.objects.create(
            course=self.course,
            child=self.child,
            is_active=True
        )
        
        # Cannot create another with same course+child
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Enrollment.objects.create(
                    course=self.course,
                    child=self.child,
                    is_active=False  # Even with different is_active
                )


class EnrollmentMultipleCoursesTests(TestCase):
    """Test enrolling a child in multiple different courses"""
    
    def setUp(self):
        """Set up test data"""
        self.branch = create_test_branch()
        self.family = create_test_family(branch=self.branch)
        self.child = create_test_child(family=self.family)
        self.course1 = create_test_course(name="Karate", branch=self.branch)
        self.course2 = create_test_course(name="Dance", branch=self.branch)
    
    def test_child_can_enroll_in_multiple_courses(self):
        """
        Test: Child can be enrolled in multiple different courses
        
        Scenario:
        - Enroll child in Course 1
        - Enroll same child in Course 2
        
        Expected:
        - Both enrollments created successfully
        - Child has 2 active enrollments
        """
        enrollment1 = Enrollment.objects.create(
            course=self.course1,
            child=self.child,
            is_active=True
        )
        
        enrollment2 = Enrollment.objects.create(
            course=self.course2,
            child=self.child,
            is_active=True
        )
        
        # Verify both enrollments exist
        self.assertEqual(Enrollment.objects.count(), 2)
        self.assertEqual(Enrollment.objects.filter(child=self.child).count(), 2)
        
        # Verify they're for different courses
        self.assertNotEqual(enrollment1.course, enrollment2.course)
        self.assertEqual(enrollment1.child, enrollment2.child)

