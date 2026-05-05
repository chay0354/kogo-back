"""
Tests for Children API Endpoints

Tests filtering, searching, and status update endpoints for children.
"""
from datetime import date, timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient
from rest_framework import status
from apps.customers.models import Child, Family, Parent
from apps.core.models import UserProfile
from .test_fixtures import (
    create_test_child,
    create_test_family,
    create_test_branch,
    create_test_course
)
from apps.enrollments.models import Enrollment


class ChildrenListAPITests(TestCase):
    """Test children list endpoint with filtering"""
    
    def setUp(self):
        """Set up test data"""
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
        self.branch1 = create_test_branch(name="Branch A")
        self.branch2 = create_test_branch(name="Branch B")
        
        # Create families in different branches
        self.family1 = create_test_family(name="Family One", branch=self.branch1)
        self.family2 = create_test_family(name="Family Two", branch=self.branch2)
        
        # Create children with different statuses
        # Calculate exact birth date for 8 years old (account for leap years)
        today = date.today()
        birth_date_8yo = date(today.year - 8, today.month, today.day)
        
        self.child_active = create_test_child(
            family=self.family1,
            first_name="Active",
            last_name="Child",
            id_number="111111111",
            status="active",
            birth_date=birth_date_8yo
        )
        
        birth_date_10yo = date(today.year - 10, today.month, today.day)
        birth_date_12yo = date(today.year - 12, today.month, today.day)
        
        self.child_trial = create_test_child(
            family=self.family1,
            first_name="Trial",
            last_name="Child",
            id_number="222222222",
            status="trial",
            birth_date=birth_date_10yo
        )
        
        self.child_payment_problem = create_test_child(
            family=self.family2,
            first_name="Payment",
            last_name="Problem",
            id_number="333333333",
            status="payment_problem",
            birth_date=birth_date_12yo
        )
    
    def test_children_list_all(self):
        """
        Test: GET /api/v1/customers/children/ returns all children
        
        Scenario:
        - Multiple children exist
        - GET without filters
        
        Expected:
        - Returns 200 OK
        - Returns all children
        - Each child includes details (name, age, family, status, enrollments)
        """
        url = '/api/v1/customers/children/'
        response = self.client.get(url)
        
        # Verify response
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Get results (handle both paginated and non-paginated responses)
        if 'results' in response.data:
            children = response.data['results']
        else:
            children = response.data
        
        self.assertEqual(len(children), 3)
        
        # Verify children data includes required fields
        child_names = [f"{c['first_name']} {c['last_name']}" for c in children]
        self.assertIn("Active Child", child_names)
        self.assertIn("Trial Child", child_names)
        self.assertIn("Payment Problem", child_names)
    
    def test_children_filter_by_status(self):
        """
        Test: GET with ?status=active returns only active children
        
        Scenario:
        - Children with different statuses exist
        - Filter by status=active
        
        Expected:
        - Returns only active children
        - Other statuses excluded
        """
        url = '/api/v1/customers/children/?status=active'
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        if 'results' in response.data:
            children = response.data['results']
        else:
            children = response.data
        
        # Should only return active child
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['status'], 'active')
        self.assertEqual(children[0]['first_name'], 'Active')
    
    def test_children_filter_by_branch(self):
        """
        Test: GET with ?branch={uuid} returns children from that branch
        
        Scenario:
        - Children in different branches
        - Filter by branch ID
        
        Expected:
        - Returns only children from specified branch (through lesson enrollments)
        """
        # The API filters by lesson_enrollments__lesson__branch_id
        # So we need to create lessons and enrollments for the test to work
        from apps.courses.models import Lesson
        from apps.enrollments.models import LessonEnrollment
        from datetime import time
        
        course = create_test_course(branch=self.branch1)
        lesson_branch1 = Lesson.objects.create(
            course=course,
            branch=self.branch1,
            day_of_week=0,
            start_time=time(16, 0),
            end_time=time(17, 0),
            status='scheduled'
        )
        
        # Enroll the two branch1 children in the lesson
        LessonEnrollment.objects.create(
            lesson=lesson_branch1,
            child=self.child_active,
            status='active'
        )
        LessonEnrollment.objects.create(
            lesson=lesson_branch1,
            child=self.child_trial,
            status='active'
        )
        
        url = f'/api/v1/customers/children/?branch={self.branch1.id}'
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        if 'results' in response.data:
            children = response.data['results']
        else:
            children = response.data
        
        # Should return 2 children from branch1 (who have enrollments in branch1 lessons)
        self.assertEqual(len(children), 2)
    
    def test_children_filter_by_age(self):
        """
        Test: GET with ?age=7-9 returns children aged 7-9
        
        Scenario:
        - Children of different ages (8, 10, 12)
        - Filter by age range 7-9
        
        Expected:
        - Returns only 8-year-old child
        """
        url = '/api/v1/customers/children/?age=7-9'
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        if 'results' in response.data:
            children = response.data['results']
        else:
            children = response.data
        
        # Should return only the 8-year-old
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['age'], 8)
        self.assertEqual(children[0]['first_name'], 'Active')
    
    def test_children_search(self):
        """
        Test: GET with ?search=Trial finds matching children
        
        Scenario:
        - Search by name
        
        Expected:
        - Returns children matching search term
        """
        url = '/api/v1/customers/children/?search=Trial'
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        if 'results' in response.data:
            children = response.data['results']
        else:
            children = response.data
        
        # Should find "Trial Child"
        self.assertGreaterEqual(len(children), 1)
        
        # Verify search found the right child
        found_trial = any(c['first_name'] == 'Trial' for c in children)
        self.assertTrue(found_trial)


class ChildUpdateStatusAPITests(TestCase):
    """Test child status update endpoint"""
    
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
        self.today = date.today()
        self.next_month = self.today + timedelta(days=30)
        self.last_month = self.today - timedelta(days=30)
        
        # Create child with payment info that should make them active
        self.child = create_test_child(
            family=self.family,
            first_name="Test",
            last_name="Child",
            status="trial",  # Wrong status initially
            subscription_start_date=self.today,
            subscription_end_date=self.today + timedelta(days=365),
            paid_until_date=self.next_month
        )
    
    def test_child_update_status_action(self):
        """
        Test: POST /api/v1/customers/children/{id}/update_status/
        
        Scenario:
        - Child has 'trial' status but payment data says 'active'
        - Call update_status endpoint
        
        Expected:
        - Returns 200 OK
        - Status recalculated and updated
        - Child status changes to 'active'
        """
        # Verify initial (incorrect) status
        self.assertEqual(self.child.status, 'trial')
        
        # Call update_status endpoint
        url = f'/api/v1/customers/children/{self.child.id}/update_status/'
        response = self.client.post(url)
        
        # Verify response
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('status', response.data)
        self.assertEqual(response.data['status'], 'active')
        
        # Verify status was updated in database
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'active')


class ChildrenWithEnrollmentsAPITests(TestCase):
    """Test that children API returns enrollment data correctly"""
    
    def setUp(self):
        """Set up test data"""
        self.client = APIClient()
        # Authenticate as Manager
        User = get_user_model()
        self.user = User.objects.create_user(
            username='manager3@test.com',
            email='manager3@test.com',
            password='pass12345!',
            is_active=True,
        )
        UserProfile.objects.update_or_create(user=self.user, defaults={'role': UserProfile.ROLE_MANAGER})
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        self.branch = create_test_branch()
        self.family = create_test_family(branch=self.branch)
        self.child = create_test_child(family=self.family)
        
        # Create courses and enroll child
        self.course1 = create_test_course(name="Karate", branch=self.branch)
        self.course2 = create_test_course(name="Dance", branch=self.branch)
        
        Enrollment.objects.create(
            course=self.course1,
            child=self.child,
            is_active=True
        )
        Enrollment.objects.create(
            course=self.course2,
            child=self.child,
            is_active=True
        )
    
    def test_children_list_includes_enrollments(self):
        """
        Test: Children list includes enrollment data
        
        Scenario:
        - Child enrolled in 2 courses
        - GET child list
        
        Expected:
        - Response includes enrollments array
        - Enrollments include course names
        """
        url = '/api/v1/customers/children/'
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        if 'results' in response.data:
            children = response.data['results']
        else:
            children = response.data
        
        # Find our child
        child_data = next(c for c in children if c['id'] == str(self.child.id))
        
        # Verify enrollments included
        self.assertIn('enrollments', child_data)
        self.assertEqual(len(child_data['enrollments']), 2)
        
        # Verify course names included
        course_names = [e['course_name'] for e in child_data['enrollments']]
        self.assertIn('Karate', course_names)
        self.assertIn('Dance', course_names)


class ChildrenCombinedFiltersTests(TestCase):
    """Test combining multiple filters"""
    
    def setUp(self):
        """Set up test data"""
        self.client = APIClient()
        # Authenticate as Manager
        User = get_user_model()
        self.user = User.objects.create_user(
            username='manager4@test.com',
            email='manager4@test.com',
            password='pass12345!',
            is_active=True,
        )
        UserProfile.objects.update_or_create(user=self.user, defaults={'role': UserProfile.ROLE_MANAGER})
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        self.branch = create_test_branch()
        self.family = create_test_family(branch=self.branch)
        
        # Calculate exact birth dates
        today = date.today()
        birth_date_8yo = date(today.year - 8, today.month, today.day)
        birth_date_12yo = date(today.year - 12, today.month, today.day)
        
        # Create children with different combinations
        self.child1 = create_test_child(
            family=self.family,
            first_name="Alice",
            id_number="AAA",
            status="active",
            birth_date=birth_date_8yo
        )
        
        self.child2 = create_test_child(
            family=self.family,
            first_name="Bob",
            id_number="BBB",
            status="active",
            birth_date=birth_date_12yo
        )
        
        self.child3 = create_test_child(
            family=self.family,
            first_name="Charlie",
            id_number="CCC",
            status="trial",
            birth_date=birth_date_8yo
        )
    
    def test_filter_by_status_and_age(self):
        """
        Test: Combine status and age filters
        
        Scenario:
        - Filter by status=active AND age=7-9
        
        Expected:
        - Returns only children matching both filters
        - Alice: active + age 8 ✓
        - Bob: active + age 12 ✗
        - Charlie: trial + age 8 ✗
        """
        url = '/api/v1/customers/children/?status=active&age=7-9'
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        if 'results' in response.data:
            children = response.data['results']
        else:
            children = response.data
        
        # Should return only Alice
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['first_name'], 'Alice')


class ChildDeleteCascadeTests(TestCase):
    """Test that deleting the last child also deletes the family and parents"""
    
    def setUp(self):
        """Set up test data"""
        self.client = APIClient()
        # Authenticate as Manager
        User = get_user_model()
        self.user = User.objects.create_user(
            username='manager5@test.com',
            email='manager5@test.com',
            password='pass12345!',
            is_active=True,
        )
        UserProfile.objects.update_or_create(user=self.user, defaults={'role': UserProfile.ROLE_MANAGER})
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        self.branch = create_test_branch()
    
    def test_delete_last_child_deletes_family_and_parents(self):
        """
        Test: DELETE last child also deletes family and parents
        
        Scenario:
        - Family has 1 child and 2 parents
        - Delete the child
        
        Expected:
        - Child deleted successfully
        - Family deleted automatically
        - Parents deleted automatically (cascade from family)
        """
        # Create family with parents
        family = create_test_family(name="Test Family", branch=self.branch)
        parent1 = Parent.objects.create(
            family=family,
            first_name="Parent1",
            last_name="Test",
            phone="0501234567",
            is_primary=True
        )
        parent2 = Parent.objects.create(
            family=family,
            first_name="Parent2",
            last_name="Test",
            phone="0507654321",
            is_primary=False
        )
        
        # Create single child
        child = create_test_child(
            family=family,
            first_name="Only",
            last_name="Child"
        )
        
        # Store IDs for verification
        family_id = family.id
        parent1_id = parent1.id
        parent2_id = parent2.id
        child_id = child.id
        
        # Verify initial state
        self.assertTrue(Family.objects.filter(id=family_id).exists())
        self.assertTrue(Parent.objects.filter(id=parent1_id).exists())
        self.assertTrue(Parent.objects.filter(id=parent2_id).exists())
        self.assertTrue(Child.objects.filter(id=child_id).exists())
        
        # Delete the child
        url = f'/api/v1/customers/children/{child_id}/'
        response = self.client.delete(url)
        
        # Verify response
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        
        # Verify child is deleted
        self.assertFalse(Child.objects.filter(id=child_id).exists())
        
        # Verify family is deleted
        self.assertFalse(Family.objects.filter(id=family_id).exists())
        
        # Verify parents are deleted
        self.assertFalse(Parent.objects.filter(id=parent1_id).exists())
        self.assertFalse(Parent.objects.filter(id=parent2_id).exists())
    
    def test_delete_child_keeps_family_when_siblings_exist(self):
        """
        Test: DELETE child keeps family and parents when siblings exist
        
        Scenario:
        - Family has 2 children and 1 parent
        - Delete one child
        
        Expected:
        - Deleted child removed
        - Family still exists
        - Parent still exists
        - Sibling still exists
        """
        # Create family with parent
        family = create_test_family(name="Multi-Child Family", branch=self.branch)
        parent = Parent.objects.create(
            family=family,
            first_name="Parent",
            last_name="Test",
            phone="0501234567",
            is_primary=True
        )
        
        # Create two children
        child1 = create_test_child(
            family=family,
            first_name="First",
            last_name="Child"
        )
        child2 = create_test_child(
            family=family,
            first_name="Second",
            last_name="Child"
        )
        
        # Store IDs
        family_id = family.id
        parent_id = parent.id
        child1_id = child1.id
        child2_id = child2.id
        
        # Delete first child
        url = f'/api/v1/customers/children/{child1_id}/'
        response = self.client.delete(url)
        
        # Verify response
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        
        # Verify first child is deleted
        self.assertFalse(Child.objects.filter(id=child1_id).exists())
        
        # Verify family still exists
        self.assertTrue(Family.objects.filter(id=family_id).exists())
        
        # Verify parent still exists
        self.assertTrue(Parent.objects.filter(id=parent_id).exists())
        
        # Verify second child still exists
        self.assertTrue(Child.objects.filter(id=child2_id).exists())
        
        # Verify family has exactly 1 child now
        family.refresh_from_db()
        self.assertEqual(family.children.count(), 1)

