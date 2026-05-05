"""
Tests for Branch API endpoints
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient
from rest_framework import status
from apps.core.models import City, Branch, Room, UserProfile
from apps.customers.models import Family
from apps.courses.models import CourseType, Course, Lesson
from apps.instructors.models import Instructor


class BranchAPITests(TestCase):
    """Test Branch CRUD operations"""
    
    def setUp(self):
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
        self.city = City.objects.create(name="תל אביב")
        
    def test_create_branch(self):
        """Test: POST /api/v1/core/branches/ creates a branch"""
        data = {
            'name': 'סניף מרכז',
            'city': str(self.city.id),
            'address': 'רחוב הרצל 15',
            'branch_codes': ['TLV01'],
            'cleaning_managers': ['משה לוי'],
            'monthly_cost': 10000,
            'cleaning_cost': 2000,
            'wifi_name': 'Studio_WiFi',
            'wifi_code': 'password123',
            'bluetooth_codes': ['123456'],
            'is_active': True,
        }
        
        response = self.client.post('/api/v1/core/branches/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Branch.objects.count(), 1)
        
        branch = Branch.objects.first()
        self.assertEqual(branch.name, 'סניף מרכז')
        self.assertEqual(branch.branch_codes, ['TLV01'])
        self.assertEqual(branch.cleaning_managers, ['משה לוי'])
        self.assertEqual(float(branch.monthly_cost), 10000)
        
    def test_list_branches(self):
        """Test: GET /api/v1/core/branches/ returns all branches"""
        Branch.objects.create(
            name='סניף 1',
            city=self.city,
            branch_codes=['BR01'],
            is_active=True
        )
        Branch.objects.create(
            name='סניף 2',
            city=self.city,
            branch_codes=['BR02'],
            is_active=True
        )
        
        response = self.client.get('/api/v1/core/branches/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # API returns paginated results
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(len(response.data['results']), 2)
        
    def test_list_branches_with_stats(self):
        """Test: GET /api/v1/core/branches/?with_stats=true returns statistics"""
        branch = Branch.objects.create(
            name='סניף מרכז',
            city=self.city,
            is_active=True
        )
        
        # Create related data
        Family.objects.create(name='משפחה 1', phone='050-1234567', branch=branch)
        Family.objects.create(name='משפחה 2', phone='050-1234568', branch=branch)
        
        course_type = CourseType.objects.create(name='קפואירה')
        Course.objects.create(
            name='מתחילים',
            course_type=course_type,
            price=350,
            capacity=20,
            branch=branch,
            is_active=True
        )
        
        Room.objects.create(name='סטודיו 1', branch=branch, capacity=20, is_active=True)
        
        response = self.client.get('/api/v1/core/branches/?with_stats=true')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # API returns paginated results
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(len(response.data['results']), 1)
        
        branch_data = response.data['results'][0]
        self.assertEqual(branch_data['families_count'], 2)
        self.assertEqual(branch_data['courses_count'], 1)
        self.assertEqual(branch_data['rooms_count'], 1)
        
    def test_retrieve_branch_detail(self):
        """Test: GET /api/v1/core/branches/{id}/ returns branch with rooms"""
        branch = Branch.objects.create(
            name='סניף מרכז',
            city=self.city,
            branch_codes=['TLV01', 'MAIN'],
            is_active=True
        )
        Room.objects.create(name='סטודיו 1', branch=branch, capacity=20, is_active=True)
        Room.objects.create(name='סטודיו 2', branch=branch, capacity=25, is_active=True)
        
        response = self.client.get(f'/api/v1/core/branches/{branch.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['name'], 'סניף מרכז')
        self.assertEqual(len(response.data['rooms']), 2)
        self.assertEqual(response.data['rooms_count'], 2)
        
    def test_update_branch(self):
        """Test: PATCH /api/v1/core/branches/{id}/ updates branch"""
        branch = Branch.objects.create(
            name='סניף ישן',
            city=self.city,
            is_active=True
        )
        
        data = {
            'name': 'סניף חדש',
            'branch_codes': ['NEW01'],
        }
        
        response = self.client.patch(
            f'/api/v1/core/branches/{branch.id}/',
            data,
            format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        branch.refresh_from_db()
        self.assertEqual(branch.name, 'סניף חדש')
        self.assertEqual(branch.branch_codes, ['NEW01'])
        
    def test_soft_delete_branch(self):
        """Test: DELETE /api/v1/core/branches/{id}/ soft deletes (sets is_active=False)"""
        branch = Branch.objects.create(
            name='סניף למחיקה',
            city=self.city,
            is_active=True
        )
        
        response = self.client.delete(f'/api/v1/core/branches/{branch.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        
        branch.refresh_from_db()
        self.assertFalse(branch.is_active)
        self.assertEqual(Branch.objects.count(), 1)  # Still exists in DB


class BranchStatisticsAPITests(TestCase):
    """Test Branch statistics endpoint"""
    
    def setUp(self):
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
        self.city = City.objects.create(name="תל אביב")
        self.branch = Branch.objects.create(
            name='סניף מרכז',
            city=self.city,
            monthly_cost=10000,
            cleaning_cost=2000,
            is_active=True
        )
        
    def test_branch_statistics_endpoint(self):
        """Test: GET /api/v1/core/branches/{id}/statistics/ returns detailed stats"""
        # Create test data
        Family.objects.create(name='משפחה 1', phone='050-1234567', branch=self.branch)
        
        course_type = CourseType.objects.create(name='קפואירה')
        course = Course.objects.create(
            name='מתחילים',
            course_type=course_type,
            price=350,
            capacity=20,
            branch=self.branch,
            is_active=True
        )
        
        room = Room.objects.create(name='סטודיו 1', branch=self.branch, capacity=20, is_active=True)
        
        instructor = Instructor.objects.create(
            first_name='יוסי',
            last_name='כהן',
            phone='050-1111111',
            email='yossi@example.com',
            primary_branch=self.branch,
            is_active=True
        )
        
        Lesson.objects.create(
            course=course,
            branch=self.branch,
            room=room,
            instructor=instructor,
            day_of_week=0,
            start_time='16:00',
            end_time='17:00',
            status='scheduled'
        )
        
        response = self.client.get(f'/api/v1/core/branches/{self.branch.id}/statistics/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        data = response.data
        self.assertEqual(data['branch_name'], 'סניף מרכז')
        self.assertEqual(data['families_count'], 1)
        self.assertEqual(data['courses_count'], 1)
        self.assertEqual(data['lessons_count'], 1)
        self.assertEqual(data['instructors_count'], 1)
        self.assertEqual(data['rooms_count'], 1)
        self.assertIn('monthly_revenue', data)
        self.assertIn('monthly_costs', data)
        self.assertIn('profit', data)


class RoomAPITests(TestCase):
    """Test Room CRUD operations"""
    
    def setUp(self):
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
        self.city = City.objects.create(name="תל אביב")
        self.branch = Branch.objects.create(
            name='סניף מרכז',
            city=self.city,
            is_active=True
        )
        
    def test_create_room(self):
        """Test: POST /api/v1/core/rooms/ creates a room"""
        data = {
            'branch': str(self.branch.id),
            'name': 'סטודיו 1',
            'capacity': 25,
            'purpose': 'ריקוד',
            'notes': 'סטודיו גדול',
            'is_active': True,
        }
        
        response = self.client.post('/api/v1/core/rooms/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Room.objects.count(), 1)
        
        room = Room.objects.first()
        self.assertEqual(room.name, 'סטודיו 1')
        self.assertEqual(room.purpose, 'ריקוד')
        self.assertEqual(room.capacity, 25)
        
    def test_list_rooms_by_branch(self):
        """Test: GET /api/v1/core/rooms/?branch_id={id} filters by branch"""
        branch2 = Branch.objects.create(name='סניף 2', city=self.city, is_active=True)
        
        Room.objects.create(name='חדר 1', branch=self.branch, capacity=20, is_active=True)
        Room.objects.create(name='חדר 2', branch=self.branch, capacity=25, is_active=True)
        Room.objects.create(name='חדר 3', branch=branch2, capacity=30, is_active=True)
        
        response = self.client.get(f'/api/v1/core/rooms/?branch_id={self.branch.id}')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # API returns paginated results
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(len(response.data['results']), 2)
        
    def test_soft_delete_room(self):
        """Test: DELETE /api/v1/core/rooms/{id}/ soft deletes room"""
        room = Room.objects.create(
            name='חדר למחיקה',
            branch=self.branch,
            capacity=20,
            is_active=True
        )
        
        response = self.client.delete(f'/api/v1/core/rooms/{room.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        
        room.refresh_from_db()
        self.assertFalse(room.is_active)


class CityAPITests(TestCase):
    """Test City CRUD operations"""
    
    def setUp(self):
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
        
    def test_create_city(self):
        """Test: POST /api/v1/core/cities/ creates a city"""
        data = {'name': 'ירושלים'}
        
        response = self.client.post('/api/v1/core/cities/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(City.objects.count(), 1)
        self.assertEqual(City.objects.first().name, 'ירושלים')
        
    def test_list_cities(self):
        """Test: GET /api/v1/core/cities/ returns all cities"""
        City.objects.create(name='תל אביב')
        City.objects.create(name='חיפה')
        City.objects.create(name='באר שבע')
        
        response = self.client.get('/api/v1/core/cities/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # API returns paginated results
        self.assertEqual(response.data['count'], 3)
        self.assertEqual(len(response.data['results']), 3)

