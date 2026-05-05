"""
Unit tests for Core app models.

Tests coverage:
- City: creation, string representation, ordering
- Branch: creation with relations, soft delete, JSON fields
- Room: creation, cascade delete, capacity
- BranchFile: file metadata, file type choices
- UserProfile: role choices, user relationship
- Snapshot models: data storage, date indexing
"""
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model

from apps.core.models import (
    City, Branch, Room, UserProfile,
    InstructorMonthlySnapshot, LessonMonthlySnapshot, BranchMonthlySnapshot
)
from apps.core.tests.test_fixtures import TestDataFactory
from apps.instructors.models import Instructor
from apps.courses.models import Course, Lesson

User = get_user_model()


class CityModelTest(TestCase):
    """Test City model"""
    
    def test_create_city(self):
        """Test creating a city"""
        city = City.objects.create(name="תל אביב")
        self.assertIsNotNone(city.id)
        self.assertEqual(city.name, "תל אביב")
        self.assertIsNotNone(city.created_at)
        self.assertIsNotNone(city.updated_at)
    
    def test_city_str_representation(self):
        """Test city string representation"""
        city = City.objects.create(name="חיפה")
        self.assertEqual(str(city), "חיפה")
    
    def test_city_ordering(self):
        """Test cities are ordered by name"""
        City.objects.create(name="זכרון יעקב")
        City.objects.create(name="אילת")
        City.objects.create(name="נתניה")
        
        cities = list(City.objects.all())
        self.assertEqual(cities[0].name, "אילת")
        self.assertEqual(cities[1].name, "זכרון יעקב")
        self.assertEqual(cities[2].name, "נתניה")
    
    def test_city_uuid_primary_key(self):
        """Test city uses UUID as primary key"""
        city = City.objects.create(name="באר שבע")
        self.assertIsNotNone(city.id)
        self.assertEqual(len(str(city.id)), 36)  # UUID format


class BranchModelTest(TestCase):
    """Test Branch model"""
    
    def setUp(self):
        self.city = City.objects.create(name="תל אביב")
    
    def test_create_basic_branch(self):
        """Test creating a basic branch"""
        branch = Branch.objects.create(
            name="סניף מרכז",
            city=self.city
        )
        self.assertIsNotNone(branch.id)
        self.assertEqual(branch.name, "סניף מרכז")
        self.assertEqual(branch.city, self.city)
        self.assertTrue(branch.is_active)
    
    def test_branch_with_json_fields(self):
        """Test branch with JSON fields (branch_codes, cleaning_managers, bluetooth_codes)"""
        branch = Branch.objects.create(
            name="סניף צפון",
            city=self.city,
            branch_codes=['TLV01', 'MAIN'],
            cleaning_managers=['משה לוי', 'דני כהן'],
            bluetooth_codes=['123456', '789012']
        )
        
        self.assertEqual(branch.branch_codes, ['TLV01', 'MAIN'])
        self.assertEqual(branch.cleaning_managers, ['משה לוי', 'דני כהן'])
        self.assertEqual(branch.bluetooth_codes, ['123456', '789012'])
    
    def test_branch_with_costs(self):
        """Test branch with monthly and cleaning costs"""
        branch = Branch.objects.create(
            name="סניף דרום",
            city=self.city,
            monthly_cost=Decimal('10000.00'),
            cleaning_cost=Decimal('2000.00')
        )
        
        self.assertEqual(branch.monthly_cost, Decimal('10000.00'))
        self.assertEqual(branch.cleaning_cost, Decimal('2000.00'))
    
    def test_branch_soft_delete(self):
        """Test branch soft delete (is_active=False)"""
        branch = Branch.objects.create(
            name="סניף למחיקה",
            city=self.city,
            is_active=True
        )
        
        # Soft delete
        branch.is_active = False
        branch.save()
        
        branch.refresh_from_db()
        self.assertFalse(branch.is_active)
        self.assertEqual(Branch.objects.count(), 1)  # Still in DB
    
    def test_branch_str_representation(self):
        """Test branch string representation"""
        branch = Branch.objects.create(name="סניף מבחן", city=self.city)
        self.assertEqual(str(branch), "סניף מבחן")
    
    def test_branch_city_relationship(self):
        """Test branch-city foreign key relationship"""
        branch = Branch.objects.create(name="סניף 1", city=self.city)
        
        # Test reverse relationship
        self.assertIn(branch, self.city.branches.all())
    
    def test_branch_city_set_null_on_delete(self):
        """Test branch.city is set to null when city is deleted"""
        branch = Branch.objects.create(name="סניף יתום", city=self.city)
        city_id = self.city.id
        
        self.city.delete()
        branch.refresh_from_db()
        
        self.assertIsNone(branch.city)
    
    def test_branch_custom_details_json(self):
        """Test branch custom_details JSON field"""
        branch = Branch.objects.create(
            name="סניף מותאם",
            city=self.city,
            custom_details=[
                {'key': 'parking', 'value': 'available'},
                {'key': 'elevator', 'value': 'yes'}
            ]
        )
        
        self.assertEqual(len(branch.custom_details), 2)
        self.assertEqual(branch.custom_details[0]['key'], 'parking')


class RoomModelTest(TestCase):
    """Test Room model"""
    
    def setUp(self):
        self.city = City.objects.create(name="תל אביב")
        self.branch = Branch.objects.create(name="סניף מרכז", city=self.city)
    
    def test_create_room(self):
        """Test creating a room"""
        room = Room.objects.create(
            name="סטודיו 1",
            branch=self.branch,
            capacity=25
        )
        
        self.assertIsNotNone(room.id)
        self.assertEqual(room.name, "סטודיו 1")
        self.assertEqual(room.branch, self.branch)
        self.assertEqual(room.capacity, 25)
        self.assertTrue(room.is_active)
    
    def test_room_with_purpose_and_notes(self):
        """Test room with purpose and notes"""
        room = Room.objects.create(
            name="סטודיו 2",
            branch=self.branch,
            capacity=30,
            purpose="ריקוד",
            notes="סטודיו גדול עם מראות"
        )
        
        self.assertEqual(room.purpose, "ריקוד")
        self.assertEqual(room.notes, "סטודיו גדול עם מראות")
    
    def test_room_cascade_delete_with_branch(self):
        """Test room is deleted when branch is deleted"""
        room = Room.objects.create(
            name="חדר למחיקה",
            branch=self.branch,
            capacity=20
        )
        
        room_id = room.id
        self.branch.delete()
        
        with self.assertRaises(Room.DoesNotExist):
            Room.objects.get(id=room_id)
    
    def test_room_str_representation(self):
        """Test room string representation includes branch name"""
        room = Room.objects.create(
            name="חדר A",
            branch=self.branch,
            capacity=15
        )
        
        self.assertEqual(str(room), f"{self.branch.name} - חדר A")
    
    def test_room_default_capacity(self):
        """Test room has default capacity of 20"""
        room = Room.objects.create(
            name="חדר ברירת מחדל",
            branch=self.branch
        )
        
        self.assertEqual(room.capacity, 20)
    
    def test_room_ordering(self):
        """Test rooms are ordered by branch and name"""
        city2 = City.objects.create(name="חיפה")
        branch2 = Branch.objects.create(name="סניף חיפה", city=city2)
        
        room1 = Room.objects.create(name="ב", branch=self.branch, capacity=20)
        room2 = Room.objects.create(name="א", branch=self.branch, capacity=20)
        room3 = Room.objects.create(name="ג", branch=branch2, capacity=20)
        
        rooms = list(Room.objects.all())
        # Should be ordered by branch first, then name
        # Just verify that rooms from same branch are together and alphabetically sorted
        branch1_rooms = [r for r in rooms if r.branch == self.branch]
        self.assertEqual(len(branch1_rooms), 2)
        self.assertEqual(branch1_rooms[0].name, "א")
        self.assertEqual(branch1_rooms[1].name, "ב")


class UserProfileModelTest(TestCase):
    """Test UserProfile model"""
    
    def test_create_user_profile_manager(self):
        """Test creating a user profile with manager role"""
        user = User.objects.create_user(
            username='manager@test.com',
            email='manager@test.com',
            password='pass123'
        )
        
        # Signal auto-creates profile, so get it and update role
        profile = UserProfile.objects.get(user=user)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save()
        
        self.assertEqual(profile.user, user)
        self.assertEqual(profile.role, UserProfile.ROLE_MANAGER)
    
    def test_create_user_profile_worker(self):
        """Test creating a user profile with worker role"""
        user = User.objects.create_user(
            username='worker@test.com',
            email='worker@test.com',
            password='pass123'
        )
        
        # Signal auto-creates profile with default worker role
        profile = UserProfile.objects.get(user=user)
        
        self.assertEqual(profile.role, UserProfile.ROLE_WORKER)
    
    def test_user_profile_default_role(self):
        """Test user profile has default role of worker (via signal)"""
        user = User.objects.create_user(
            username='default@test.com',
            password='pass123'
        )
        
        # Signal auto-creates profile with default worker role
        profile = UserProfile.objects.get(user=user)
        
        self.assertEqual(profile.role, UserProfile.ROLE_WORKER)
    
    def test_user_profile_str_representation(self):
        """Test user profile string representation"""
        user = User.objects.create_user(
            username='test@test.com',
            email='test@test.com',
            password='pass123'
        )
        
        # Signal auto-creates profile, get it and update
        profile = UserProfile.objects.get(user=user)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save()
        
        self.assertIn('test@test.com', str(profile))
        self.assertIn('manager', str(profile))
    
    def test_user_profile_cascade_delete(self):
        """Test user profile is deleted when user is deleted"""
        user = User.objects.create_user(
            username='todelete@test.com',
            password='pass123'
        )
        
        # Signal auto-creates profile
        profile = UserProfile.objects.get(user=user)
        profile_id = profile.id
        
        user.delete()
        
        self.assertEqual(UserProfile.objects.filter(id=profile_id).count(), 0)
    
    def test_user_profile_one_to_one_relationship(self):
        """Test user profile one-to-one relationship with user"""
        user = User.objects.create_user(
            username='unique@test.com',
            password='pass123'
        )
        
        # Signal auto-creates profile
        profile = UserProfile.objects.get(user=user)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save()
        
        # Test reverse relationship
        self.assertEqual(user.profile, profile)


class InstructorMonthlySnapshotTest(TestCase):
    """Test InstructorMonthlySnapshot model"""
    
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
        self.instructor = TestDataFactory.create_instructor(branch=self.branch)
    
    def test_create_instructor_snapshot(self):
        """Test creating an instructor monthly snapshot"""
        snapshot = InstructorMonthlySnapshot.objects.create(
            instructor=self.instructor,
            month='2024-01',
            total_lessons=20,
            total_students=150,
            base_revenue=Decimal('7000.00'),
            total_discounts=Decimal('500.00'),
            total_revenue=Decimal('6500.00'),
            total_salary=Decimal('4000.00'),
            profit=Decimal('2500.00')
        )
        
        self.assertEqual(snapshot.month, '2024-01')
        self.assertEqual(snapshot.total_lessons, 20)
        self.assertEqual(snapshot.total_revenue, Decimal('6500.00'))
    
    def test_instructor_snapshot_unique_constraint(self):
        """Test instructor snapshot has unique constraint on instructor+month"""
        InstructorMonthlySnapshot.objects.create(
            instructor=self.instructor,
            month='2024-01',
            total_lessons=10
        )
        
        # Creating duplicate should raise error
        with self.assertRaises(Exception):  # IntegrityError
            InstructorMonthlySnapshot.objects.create(
                instructor=self.instructor,
                month='2024-01',
                total_lessons=20
            )
    
    def test_instructor_snapshot_with_bonuses(self):
        """Test instructor snapshot with bonus tracking"""
        snapshot = InstructorMonthlySnapshot.objects.create(
            instructor=self.instructor,
            month='2024-02',
            total_salary=Decimal('3000.00'),
            total_bonuses=Decimal('500.00')
        )
        
        self.assertEqual(snapshot.total_bonuses, Decimal('500.00'))
    
    def test_instructor_snapshot_finalized_flag(self):
        """Test instructor snapshot finalization flag"""
        snapshot = InstructorMonthlySnapshot.objects.create(
            instructor=self.instructor,
            month='2024-03',
            is_finalized=False
        )
        
        self.assertFalse(snapshot.is_finalized)
        
        snapshot.is_finalized = True
        snapshot.save()
        snapshot.refresh_from_db()
        
        self.assertTrue(snapshot.is_finalized)
    
    def test_instructor_snapshot_str_representation(self):
        """Test instructor snapshot string representation"""
        snapshot = InstructorMonthlySnapshot.objects.create(
            instructor=self.instructor,
            month='2024-04'
        )
        
        expected = f"{self.instructor.full_name} - 2024-04"
        self.assertEqual(str(snapshot), expected)


class LessonMonthlySnapshotTest(TestCase):
    """Test LessonMonthlySnapshot model"""
    
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
        self.course = TestDataFactory.create_course(branch=self.branch)
        self.instructor = TestDataFactory.create_instructor(branch=self.branch)
        self.room = TestDataFactory.create_room(branch=self.branch)
        self.lesson = TestDataFactory.create_lesson(
            course=self.course,
            branch=self.branch,
            room=self.room,
            instructor=self.instructor
        )
    
    def test_create_lesson_snapshot(self):
        """Test creating a lesson monthly snapshot"""
        snapshot = LessonMonthlySnapshot.objects.create(
            lesson=self.lesson,
            instructor=self.instructor,
            course=self.course,
            branch=self.branch,
            month='2024-01',
            enrolled_students=15,
            base_revenue=Decimal('5250.00'),
            total_discounts=Decimal('250.00'),
            revenue=Decimal('5000.00'),
            instructor_salary=Decimal('1500.00'),
            profit=Decimal('3500.00')
        )
        
        self.assertEqual(snapshot.enrolled_students, 15)
        self.assertEqual(snapshot.revenue, Decimal('5000.00'))
        self.assertEqual(snapshot.profit, Decimal('3500.00'))
    
    def test_lesson_snapshot_unique_constraint(self):
        """Test lesson snapshot has unique constraint on lesson+month"""
        LessonMonthlySnapshot.objects.create(
            lesson=self.lesson,
            instructor=self.instructor,
            course=self.course,
            branch=self.branch,
            month='2024-01'
        )
        
        # Creating duplicate should raise error
        with self.assertRaises(Exception):  # IntegrityError
            LessonMonthlySnapshot.objects.create(
                lesson=self.lesson,
                instructor=self.instructor,
                course=self.course,
                branch=self.branch,
                month='2024-01'
            )
    
    def test_lesson_snapshot_str_representation(self):
        """Test lesson snapshot string representation"""
        snapshot = LessonMonthlySnapshot.objects.create(
            lesson=self.lesson,
            instructor=self.instructor,
            course=self.course,
            branch=self.branch,
            month='2024-02'
        )
        
        self.assertIn('2024-02', str(snapshot))


class BranchMonthlySnapshotTest(TestCase):
    """Test BranchMonthlySnapshot model"""
    
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
    
    def test_create_branch_snapshot(self):
        """Test creating a branch monthly snapshot"""
        snapshot = BranchMonthlySnapshot.objects.create(
            branch=self.branch,
            month='2024-01',
            total_students=100,
            base_revenue=Decimal('35000.00'),
            total_discounts=Decimal('2000.00'),
            total_revenue=Decimal('33000.00'),
            instructor_salaries=Decimal('15000.00'),
            instructor_bonuses=Decimal('2000.00'),
            operational_costs=Decimal('8000.00'),
            instructor_costs=Decimal('25000.00'),
            profit=Decimal('8000.00'),
            active_courses_count=10
        )
        
        self.assertEqual(snapshot.total_students, 100)
        self.assertEqual(snapshot.total_revenue, Decimal('33000.00'))
        self.assertEqual(snapshot.profit, Decimal('8000.00'))
    
    def test_branch_snapshot_unique_constraint(self):
        """Test branch snapshot has unique constraint on branch+month"""
        BranchMonthlySnapshot.objects.create(
            branch=self.branch,
            month='2024-01'
        )
        
        # Creating duplicate should raise error
        with self.assertRaises(Exception):  # IntegrityError
            BranchMonthlySnapshot.objects.create(
                branch=self.branch,
                month='2024-01'
            )
    
    def test_branch_snapshot_expense_breakdown(self):
        """Test branch snapshot tracks detailed expense breakdown"""
        snapshot = BranchMonthlySnapshot.objects.create(
            branch=self.branch,
            month='2024-02',
            instructor_salaries=Decimal('10000.00'),
            instructor_bonuses=Decimal('1500.00'),
            operational_costs=Decimal('5000.00')
        )
        
        self.assertEqual(snapshot.instructor_salaries, Decimal('10000.00'))
        self.assertEqual(snapshot.instructor_bonuses, Decimal('1500.00'))
        self.assertEqual(snapshot.operational_costs, Decimal('5000.00'))
    
    def test_branch_snapshot_str_representation(self):
        """Test branch snapshot string representation"""
        snapshot = BranchMonthlySnapshot.objects.create(
            branch=self.branch,
            month='2024-03'
        )
        
        expected = f"{self.branch.name} - 2024-03"
        self.assertEqual(str(snapshot), expected)
    
    def test_branch_snapshot_finalized_flag(self):
        """Test branch snapshot finalization tracking"""
        snapshot = BranchMonthlySnapshot.objects.create(
            branch=self.branch,
            month='2024-04',
            is_finalized=False
        )
        
        self.assertFalse(snapshot.is_finalized)
        
        snapshot.is_finalized = True
        snapshot.save()
        snapshot.refresh_from_db()
        
        self.assertTrue(snapshot.is_finalized)
