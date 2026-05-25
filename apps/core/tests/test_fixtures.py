"""
Shared test fixtures and utilities for backend tests.

This module provides:
- Factory methods for creating test data
- Base test classes with common setup
- Mock helpers for external services
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient
from unittest.mock import MagicMock
from decimal import Decimal
from datetime import date, time, timedelta

from apps.core.models import City, Branch, Room, UserProfile
from apps.customers.models import Family, Parent, Child
from apps.courses.models import CourseType, Course, Lesson
from apps.instructors.models import Instructor


User = get_user_model()


class TestDataFactory:
    """Factory methods for creating test data"""
    
    @staticmethod
    def create_city(name="תל אביב"):
        """Create a test city"""
        return City.objects.create(name=name)
    
    @staticmethod
    def create_branch(name="סניף מרכז", city=None, **kwargs):
        """Create a test branch"""
        if city is None:
            city = TestDataFactory.create_city()
        
        defaults = {
            'is_active': True,
            'branch_codes': ['TEST01'],
            'monthly_cost': Decimal('10000.00'),
            'cleaning_cost': Decimal('2000.00'),
        }
        defaults.update(kwargs)
        
        return Branch.objects.create(
            name=name,
            city=city,
            **defaults
        )
    
    @staticmethod
    def create_room(name="סטודיו 1", branch=None, **kwargs):
        """Create a test room"""
        if branch is None:
            branch = TestDataFactory.create_branch()
        
        defaults = {
            'capacity': 20,
            'is_active': True,
        }
        defaults.update(kwargs)
        
        return Room.objects.create(
            name=name,
            branch=branch,
            **defaults
        )
    
    @staticmethod
    def create_user(username="testuser@example.com", role=UserProfile.ROLE_MANAGER, **kwargs):
        """Create a test user with profile"""
        defaults = {
            'email': username,
            'password': 'testpass123!',
            'is_active': True,
        }
        defaults.update(kwargs)
        
        user = User.objects.create_user(username=username, **defaults)
        UserProfile.objects.update_or_create(
            user=user,
            defaults={'role': role}
        )
        return user
    
    @staticmethod
    def create_family(name="משפחה בדיקה", branch=None, **kwargs):
        """Create a test family"""
        if branch is None:
            branch = TestDataFactory.create_branch()
        
        defaults = {
            'phone': '050-1234567',
        }
        defaults.update(kwargs)
        
        return Family.objects.create(
            name=name,
            branch=branch,
            **defaults
        )
    
    @staticmethod
    def create_parent(family=None, first_name="יוסי", last_name="כהן", **kwargs):
        """Create a test parent"""
        if family is None:
            family = TestDataFactory.create_family()
        
        defaults = {
            'phone': '050-1111111',
            'is_primary': True,
        }
        defaults.update(kwargs)
        
        return Parent.objects.create(
            family=family,
            first_name=first_name,
            last_name=last_name,
            **defaults
        )
    
    @staticmethod
    def create_child(family=None, first_name="דני", last_name="כהן", **kwargs):
        """Create a test child"""
        if family is None:
            family = TestDataFactory.create_family()
        
        defaults = {
            'birth_date': date.today() - timedelta(days=365*8),  # 8 years old
            'gender': 'male',
            'status': 'pending',
        }
        defaults.update(kwargs)
        
        return Child.objects.create(
            family=family,
            first_name=first_name,
            last_name=last_name,
            **defaults
        )
    
    @staticmethod
    def create_course_type(name="קפואירה"):
        """Create a test course type"""
        return CourseType.objects.create(name=name)
    
    @staticmethod
    def create_course(name="מתחילים", branch=None, course_type=None, **kwargs):
        """Create a test course"""
        if branch is None:
            branch = TestDataFactory.create_branch()
        if course_type is None:
            course_type = TestDataFactory.create_course_type()
        
        defaults = {
            'price': Decimal('350.00'),
            'capacity': 20,
            'is_active': True,
        }
        defaults.update(kwargs)
        
        return Course.objects.create(
            name=name,
            branch=branch,
            course_type=course_type,
            **defaults
        )
    
    @staticmethod
    def create_instructor(first_name="משה", last_name="לוי", branch=None, **kwargs):
        """Create a test instructor"""
        if branch is None:
            branch = TestDataFactory.create_branch()
        
        defaults = {
            'phone': '050-2222222',
            'email': f'{first_name.lower()}@example.com',
            'is_active': True,
        }
        defaults.update(kwargs)
        
        return Instructor.objects.create(
            first_name=first_name,
            last_name=last_name,
            primary_branch=branch,
            **defaults
        )
    
    @staticmethod
    def create_lesson(course=None, branch=None, room=None, instructor=None, **kwargs):
        """Create a test lesson. branch is ignored (branch is now on Course)."""
        if course is None:
            course = TestDataFactory.create_course()
        # Derive branch from course for room/instructor creation
        effective_branch = branch or course.branch
        if room is None:
            room = TestDataFactory.create_room(branch=effective_branch)
        if instructor is None:
            instructor = TestDataFactory.create_instructor(branch=effective_branch)

        defaults = {
            'day_of_week': 0,  # Monday
            'start_time': time(16, 0),
            'end_time': time(17, 0),
            'status': 'scheduled',
        }
        defaults.update(kwargs)

        return Lesson.objects.create(
            course=course,
            room=room,
            instructor=instructor,
            **defaults
        )


class BaseTestCase(TestCase):
    """Base test case with common setup"""
    
    def setUp(self):
        """Set up common test data"""
        self.city = TestDataFactory.create_city()
        self.branch = TestDataFactory.create_branch(city=self.city)
        self.room = TestDataFactory.create_room(branch=self.branch)


class BaseAPITestCase(TestCase):
    """Base API test case with authentication"""
    
    def setUp(self):
        """Set up API client and authentication"""
        self.client = APIClient()
        self.user = TestDataFactory.create_user(
            username='testmanager@example.com',
            role=UserProfile.ROLE_MANAGER
        )
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        
        # Create common test data
        self.city = TestDataFactory.create_city()
        self.branch = TestDataFactory.create_branch(city=self.city)


class MockTranzilaResponse:
    """Mock Tranzila API response helper"""
    
    @staticmethod
    def success_payment_url():
        """Mock successful payment URL generation"""
        return "https://direct.tranzila.com/test/iframe.php?sum=350&currency=1&pdesc=Test"
    
    @staticmethod
    def success_webhook_response():
        """Mock successful webhook response"""
        return {
            'Response': '000',
            'TranzilaTK': 'test_token_123',
            'ConfirmationCode': 'ABC123',
            'sum': '350.00',
            'tranmode': 'V',
            'index': '1',
            'ccno': '4580****1234',
        }
    
    @staticmethod
    def failed_webhook_response():
        """Mock failed webhook response"""
        return {
            'Response': '033',  # Declined
            'TranzilaTK': '',
            'sum': '350.00',
            'tranmode': 'V',
        }
    
    @staticmethod
    def create_mock_requests_response(status_code=200, text='', json_data=None):
        """Create a mock requests response"""
        mock = MagicMock()
        mock.status_code = status_code
        mock.text = text
        if json_data:
            mock.json.return_value = json_data
        return mock
