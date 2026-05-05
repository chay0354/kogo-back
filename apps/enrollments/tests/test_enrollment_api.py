"""
Tests for Enrollment API Endpoints

Tests the REST API endpoints for enrollment creation, listing, and duplicate handling.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient
from rest_framework import status
from apps.enrollments.models import Enrollment
from apps.core.models import UserProfile
from apps.customers.tests.test_fixtures import (
    create_test_child,
    create_test_family,
    create_test_course,
    create_test_branch
)


class EnrollmentAPITests(TestCase):
    """Test enrollment API endpoints"""
    
    def setUp(self):
        """Set up test data and API client"""
        self.client = APIClient()
        # Authenticate as Manager
        User = get_user_model()
        self.user = User.objects.create_user(
            username='manager@test.com',
            email='manager@test.com',
            password='pass12345!',
            is_active=True,
        )
        UserProfile.objects.update_or_create(user=self.user, defaults={'role': UserProfile.ROLE_MANAGER})
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        self.branch = create_test_branch()
        self.family = create_test_family(branch=self.branch)
        self.child = create_test_child(family=self.family)
        self.course = create_test_course(branch=self.branch)
    
    def test_enrollment_create_endpoint(self):
        """
        Test: POST /api/v1/enrollments/enrollments/ creates enrollment
        
        Scenario:
        - POST new enrollment data
        
        Expected:
        - Returns 201 CREATED
        - Enrollment created in database
        - Response includes enrollment data
        """
        url = '/api/v1/enrollments/enrollments/'
        data = {
            'course': str(self.course.id),
            'child': str(self.child.id),
            'is_active': True
        }
        
        response = self.client.post(url, data, format='json')
        
        # Verify response
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn('id', response.data)
        # Convert UUID to string for comparison
        self.assertEqual(str(response.data['course']), str(self.course.id))
        self.assertEqual(str(response.data['child']), str(self.child.id))
        
        # Verify enrollment created in database
        self.assertEqual(Enrollment.objects.count(), 1)
        enrollment = Enrollment.objects.first()
        self.assertEqual(enrollment.course, self.course)
        self.assertEqual(enrollment.child, self.child)
        self.assertTrue(enrollment.is_active)
    
    def test_enrollment_list_endpoint(self):
        """
        Test: GET /api/v1/enrollments/enrollments/ returns all enrollments
        
        Scenario:
        - Create multiple enrollments
        - GET list endpoint
        
        Expected:
        - Returns 200 OK
        - Returns all enrollments
        - Each enrollment includes related data
        """
        # Create enrollments
        course2 = create_test_course(name="Course 2", branch=self.branch)
        child2 = create_test_child(
            family=self.family,
            first_name="Child2",
            id_number="111222333"
        )
        
        enrollment1 = Enrollment.objects.create(
            course=self.course,
            child=self.child,
            is_active=True
        )
        enrollment2 = Enrollment.objects.create(
            course=course2,
            child=child2,
            is_active=True
        )
        
        url = '/api/v1/enrollments/enrollments/'
        response = self.client.get(url)
        
        # Verify response
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Handle paginated response
        if 'results' in response.data:
            enrollments = response.data['results']
        else:
            enrollments = response.data
        
        self.assertEqual(len(enrollments), 2)
        
        # Verify enrollment data includes course and child names
        enrollment_ids = [str(e['id']) for e in enrollments]
        self.assertIn(str(enrollment1.id), enrollment_ids)
        self.assertIn(str(enrollment2.id), enrollment_ids)
    
    def test_enrollment_duplicate_error(self):
        """
        Test: POST duplicate enrollment returns 400 error
        
        Scenario:
        - Create enrollment for child+course
        - Attempt to create duplicate
        
        Expected:
        - Returns 400 BAD REQUEST
        - Error message indicates duplicate
        - No duplicate created
        """
        # Create first enrollment
        Enrollment.objects.create(
            course=self.course,
            child=self.child,
            is_active=True
        )
        
        # Attempt duplicate via API
        url = '/api/v1/enrollments/enrollments/'
        data = {
            'course': str(self.course.id),
            'child': str(self.child.id),
            'is_active': True
        }
        
        response = self.client.post(url, data, format='json')
        
        # Verify error response
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.data)
        self.assertIn('כבר רשום', response.data['error'])  # Hebrew: "already enrolled"
        
        # Verify no duplicate created
        self.assertEqual(Enrollment.objects.count(), 1)


class EnrollmentReactivationAPITests(TestCase):
    """Test enrollment reactivation through API"""
    
    def setUp(self):
        """Set up test data"""
        self.client = APIClient()
        # Authenticate as Manager
        User = get_user_model()
        self.user = User.objects.create_user(
            username='manager2@test.com',
            email='manager2@test.com',
            password='pass12345!',
            is_active=True,
        )
        UserProfile.objects.update_or_create(user=self.user, defaults={'role': UserProfile.ROLE_MANAGER})
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        self.branch = create_test_branch()
        self.family = create_test_family(branch=self.branch)
        self.child = create_test_child(family=self.family)
        self.course = create_test_course(branch=self.branch)
    
    def test_reactivate_inactive_enrollment_via_api(self):
        """
        Test: Re-enrolling in same course reactivates inactive enrollment
        
        Scenario:
        - Child has inactive enrollment
        - POST enrollment for same child+course
        
        Expected:
        - Returns 200 OK (not 201)
        - Existing enrollment reactivated
        - No duplicate created
        """
        # Create inactive enrollment
        enrollment = Enrollment.objects.create(
            course=self.course,
            child=self.child,
            is_active=False
        )
        
        original_id = enrollment.id
        
        # Re-enroll via API
        url = '/api/v1/enrollments/enrollments/'
        data = {
            'course': str(self.course.id),
            'child': str(self.child.id),
            'is_active': True
        }
        
        response = self.client.post(url, data, format='json')
        
        # Verify response (200 for reactivation, not 201)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['id'], str(original_id))
        
        # Verify no duplicate created
        self.assertEqual(Enrollment.objects.count(), 1)
        
        # Verify enrollment was reactivated
        enrollment.refresh_from_db()
        self.assertTrue(enrollment.is_active)

