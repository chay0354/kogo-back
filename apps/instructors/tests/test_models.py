"""
Unit tests for Instructors app models.

Tests coverage:
- Instructor: salary models, full_name property
- InstructorSalaryTier: tiered salary structure
- InstructorBranch: branch assignments
- InstructorBonus: bonus tracking
"""
from decimal import Decimal
from datetime import date, timedelta
from django.test import TestCase

from apps.core.tests.test_fixtures import TestDataFactory
from apps.instructors.models import Instructor, InstructorSalaryTier, InstructorBranch, InstructorBonus


class InstructorModelTest(TestCase):
    """Test Instructor model"""
    
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
    
    def test_create_instructor(self):
        """Test creating an instructor"""
        instructor = Instructor.objects.create(
            first_name="יוסי",
            last_name="כהן",
            phone="050-1234567",
            email="yossi@example.com",
            primary_branch=self.branch
        )
        
        self.assertEqual(instructor.first_name, "יוסי")
        self.assertEqual(instructor.last_name, "כהן")
        self.assertEqual(instructor.email, "yossi@example.com")
        self.assertTrue(instructor.is_active)
    
    def test_instructor_full_name_property(self):
        """Test instructor full_name property"""
        instructor = Instructor.objects.create(
            first_name="דני",
            last_name="לוי",
            phone="050-1111111",
            email="danny@example.com",
            primary_branch=self.branch
        )
        
        self.assertEqual(instructor.full_name, "דני לוי")
    
    def test_instructor_str_representation(self):
        """Test instructor string representation"""
        instructor = Instructor.objects.create(
            first_name="משה",
            last_name="דהן",
            phone="050-2222222",
            email="moshe@example.com",
            primary_branch=self.branch
        )
        
        self.assertEqual(str(instructor), "משה דהן")
    
    def test_instructor_salary_model_choices(self):
        """Test instructor salary model choices"""
        instructor1 = Instructor.objects.create(
            first_name="אבי",
            last_name="כהן",
            phone="050-1",
            email="avi@example.com",
            primary_branch=self.branch,
            salary_model_type='fixed_per_lesson'
        )
        
        instructor2 = Instructor.objects.create(
            first_name="בני",
            last_name="לוי",
            phone="050-2",
            email="beni@example.com",
            primary_branch=self.branch,
            salary_model_type='tiered_by_students'
        )
        
        self.assertEqual(instructor1.salary_model_type, 'fixed_per_lesson')
        self.assertEqual(instructor2.salary_model_type, 'tiered_by_students')
    
    def test_instructor_fixed_salary_default(self):
        """Test instructor has default fixed salary per lesson"""
        instructor = Instructor.objects.create(
            first_name="גיל",
            last_name="אבוטבול",
            phone="050-3333333",
            email="gil@example.com",
            primary_branch=self.branch
        )
        
        self.assertEqual(instructor.fixed_salary_per_lesson, Decimal('250.00'))
    
    def test_instructor_with_specialization(self):
        """Test instructor with specialization field"""
        instructor = Instructor.objects.create(
            first_name="רון",
            last_name="זבוטינסקי",
            phone="050-4444444",
            email="ron@example.com",
            primary_branch=self.branch,
            specialization="קפואירה, אקרובטיקה"
        )
        
        self.assertEqual(instructor.specialization, "קפואירה, אקרובטיקה")
    
    def test_instructor_is_active_flag(self):
        """Test instructor can be deactivated"""
        instructor = Instructor.objects.create(
            first_name="שרה",
            last_name="כהן",
            phone="050-5555555",
            email="sara@example.com",
            primary_branch=self.branch,
            is_active=True
        )
        
        instructor.is_active = False
        instructor.save()
        instructor.refresh_from_db()
        
        self.assertFalse(instructor.is_active)
    
    def test_instructor_ordering(self):
        """Test instructors are ordered by last_name, first_name"""
        Instructor.objects.create(
            first_name="זאב",
            last_name="כהן",
            phone="050-1",
            email="zeev@example.com",
            primary_branch=self.branch
        )
        
        Instructor.objects.create(
            first_name="אבי",
            last_name="לוי",
            phone="050-2",
            email="avi@example.com",
            primary_branch=self.branch
        )
        
        Instructor.objects.create(
            first_name="בני",
            last_name="כהן",
            phone="050-3",
            email="beni@example.com",
            primary_branch=self.branch
        )
        
        instructors = list(Instructor.objects.all())
        self.assertEqual(instructors[0].last_name, "כהן")
        self.assertEqual(instructors[0].first_name, "בני")  # Alphabetically first in כהן
        self.assertEqual(instructors[2].last_name, "לוי")


class InstructorSalaryTierModelTest(TestCase):
    """Test InstructorSalaryTier model"""
    
    def setUp(self):
        self.instructor = TestDataFactory.create_instructor()
    
    def test_create_salary_tier(self):
        """Test creating a salary tier"""
        tier = InstructorSalaryTier.objects.create(
            instructor=self.instructor,
            min_students=1,
            max_students=10,
            salary_per_lesson=Decimal('200.00')
        )
        
        self.assertEqual(tier.min_students, 1)
        self.assertEqual(tier.max_students, 10)
        self.assertEqual(tier.salary_per_lesson, Decimal('200.00'))
    
    def test_salary_tier_without_max(self):
        """Test salary tier without max students (open-ended)"""
        tier = InstructorSalaryTier.objects.create(
            instructor=self.instructor,
            min_students=20,
            salary_per_lesson=Decimal('400.00')
        )
        
        self.assertEqual(tier.min_students, 20)
        self.assertIsNone(tier.max_students)
    
    def test_salary_tier_str_representation_with_max(self):
        """Test salary tier string representation with max students"""
        tier = InstructorSalaryTier.objects.create(
            instructor=self.instructor,
            min_students=5,
            max_students=15,
            salary_per_lesson=Decimal('250.00')
        )
        
        str_repr = str(tier)
        self.assertIn(self.instructor.full_name, str_repr)
        self.assertIn('5-15', str_repr)
        self.assertIn('250', str_repr)
    
    def test_salary_tier_str_representation_without_max(self):
        """Test salary tier string representation without max students"""
        tier = InstructorSalaryTier.objects.create(
            instructor=self.instructor,
            min_students=20,
            salary_per_lesson=Decimal('400.00')
        )
        
        str_repr = str(tier)
        self.assertIn('20+', str_repr)
    
    def test_salary_tier_multiple_tiers(self):
        """Test instructor can have multiple salary tiers"""
        tier1 = InstructorSalaryTier.objects.create(
            instructor=self.instructor,
            min_students=1,
            max_students=10,
            salary_per_lesson=Decimal('200.00')
        )
        
        tier2 = InstructorSalaryTier.objects.create(
            instructor=self.instructor,
            min_students=11,
            max_students=20,
            salary_per_lesson=Decimal('300.00')
        )
        
        tier3 = InstructorSalaryTier.objects.create(
            instructor=self.instructor,
            min_students=21,
            salary_per_lesson=Decimal('400.00')
        )
        
        tiers = self.instructor.salary_tiers.all()
        self.assertEqual(tiers.count(), 3)
    
    def test_salary_tier_cascade_delete_with_instructor(self):
        """Test salary tier is deleted when instructor is deleted"""
        tier = InstructorSalaryTier.objects.create(
            instructor=self.instructor,
            min_students=1,
            max_students=10,
            salary_per_lesson=Decimal('200.00')
        )
        
        tier_id = tier.id
        self.instructor.delete()
        
        with self.assertRaises(InstructorSalaryTier.DoesNotExist):
            InstructorSalaryTier.objects.get(id=tier_id)


class InstructorBranchModelTest(TestCase):
    """Test InstructorBranch model"""
    
    def setUp(self):
        self.instructor = TestDataFactory.create_instructor()
        self.branch = self.instructor.primary_branch
        self.branch2 = TestDataFactory.create_branch(name="סניף 2")
    
    def test_create_instructor_branch_assignment(self):
        """Test creating an instructor-branch assignment"""
        assignment = InstructorBranch.objects.create(
            instructor=self.instructor,
            branch=self.branch2
        )
        
        self.assertEqual(assignment.instructor, self.instructor)
        self.assertEqual(assignment.branch, self.branch2)
    
    def test_instructor_branch_unique_constraint(self):
        """Test instructor-branch assignment has unique constraint"""
        InstructorBranch.objects.create(
            instructor=self.instructor,
            branch=self.branch2
        )
        
        # Creating duplicate should raise error
        with self.assertRaises(Exception):  # IntegrityError
            InstructorBranch.objects.create(
                instructor=self.instructor,
                branch=self.branch2
            )
    
    def test_instructor_multiple_branch_assignments(self):
        """Test instructor can be assigned to multiple branches"""
        branch3 = TestDataFactory.create_branch(name="סניף 3")
        
        assignment1 = InstructorBranch.objects.create(
            instructor=self.instructor,
            branch=self.branch
        )
        
        assignment2 = InstructorBranch.objects.create(
            instructor=self.instructor,
            branch=self.branch2
        )
        
        assignment3 = InstructorBranch.objects.create(
            instructor=self.instructor,
            branch=branch3
        )
        
        assignments = self.instructor.branch_assignments.all()
        self.assertEqual(assignments.count(), 3)
    
    def test_instructor_branch_str_representation(self):
        """Test instructor-branch string representation"""
        assignment = InstructorBranch.objects.create(
            instructor=self.instructor,
            branch=self.branch2
        )
        
        str_repr = str(assignment)
        self.assertIn(self.instructor.full_name, str_repr)
        self.assertIn(self.branch2.name, str_repr)
    
    def test_instructor_branch_cascade_delete_with_instructor(self):
        """Test assignment is deleted when instructor is deleted"""
        assignment = InstructorBranch.objects.create(
            instructor=self.instructor,
            branch=self.branch2
        )
        
        assignment_id = assignment.id
        self.instructor.delete()
        
        with self.assertRaises(InstructorBranch.DoesNotExist):
            InstructorBranch.objects.get(id=assignment_id)
    
    def test_instructor_branch_cascade_delete_with_branch(self):
        """Test assignment is deleted when branch is deleted"""
        assignment = InstructorBranch.objects.create(
            instructor=self.instructor,
            branch=self.branch2
        )
        
        assignment_id = assignment.id
        self.branch2.delete()
        
        with self.assertRaises(InstructorBranch.DoesNotExist):
            InstructorBranch.objects.get(id=assignment_id)


class InstructorBonusModelTest(TestCase):
    """Test InstructorBonus model"""
    
    def setUp(self):
        self.instructor = TestDataFactory.create_instructor()
    
    def test_create_instructor_bonus(self):
        """Test creating an instructor bonus"""
        bonus = InstructorBonus.objects.create(
            instructor=self.instructor,
            bonus_type='one_time',
            amount=Decimal('500.00'),
            bonus_date=date.today(),
            description="בונוס ביצועים"
        )
        
        self.assertEqual(bonus.amount, Decimal('500.00'))
        self.assertEqual(bonus.bonus_type, 'one_time')
        self.assertEqual(bonus.description, "בונוס ביצועים")
    
    def test_instructor_bonus_with_period(self):
        """Test instructor bonus with period range"""
        period_start = date(2024, 1, 1)
        period_end = date(2024, 1, 31)
        
        bonus = InstructorBonus.objects.create(
            instructor=self.instructor,
            bonus_type='one_time',
            amount=Decimal('1000.00'),
            bonus_date=date(2024, 2, 1),
            description="בונוס חודש ינואר",
            period_start=period_start,
            period_end=period_end
        )
        
        self.assertEqual(bonus.period_start, period_start)
        self.assertEqual(bonus.period_end, period_end)
    
    def test_instructor_bonus_with_notes(self):
        """Test instructor bonus with notes"""
        bonus = InstructorBonus.objects.create(
            instructor=self.instructor,
            bonus_type='one_time',
            amount=Decimal('300.00'),
            bonus_date=date.today(),
            description="בונוס",
            notes="שיפור משמעותי בהוראה"
        )
        
        self.assertEqual(bonus.notes, "שיפור משמעותי בהוראה")
    
    def test_instructor_multiple_bonuses(self):
        """Test instructor can have multiple bonuses"""
        bonus1 = InstructorBonus.objects.create(
            instructor=self.instructor,
            amount=Decimal('500.00'),
            bonus_date=date.today() - timedelta(days=60)
        )
        
        bonus2 = InstructorBonus.objects.create(
            instructor=self.instructor,
            amount=Decimal('600.00'),
            bonus_date=date.today() - timedelta(days=30)
        )
        
        bonus3 = InstructorBonus.objects.create(
            instructor=self.instructor,
            amount=Decimal('700.00'),
            bonus_date=date.today()
        )
        
        bonuses = self.instructor.bonuses.all()
        self.assertEqual(bonuses.count(), 3)
    
    def test_instructor_bonus_str_representation(self):
        """Test instructor bonus string representation"""
        bonus = InstructorBonus.objects.create(
            instructor=self.instructor,
            amount=Decimal('500.00'),
            bonus_date=date(2024, 3, 15)
        )
        
        str_repr = str(bonus)
        self.assertIn(self.instructor.full_name, str_repr)
        self.assertIn('500', str_repr)
    
    def test_instructor_bonus_ordering(self):
        """Test bonuses are ordered by -bonus_date, -created_at"""
        bonus1 = InstructorBonus.objects.create(
            instructor=self.instructor,
            amount=Decimal('500.00'),
            bonus_date=date.today() - timedelta(days=60)
        )
        
        bonus2 = InstructorBonus.objects.create(
            instructor=self.instructor,
            amount=Decimal('600.00'),
            bonus_date=date.today() - timedelta(days=30)
        )
        
        bonus3 = InstructorBonus.objects.create(
            instructor=self.instructor,
            amount=Decimal('700.00'),
            bonus_date=date.today()
        )
        
        bonuses = list(InstructorBonus.objects.filter(instructor=self.instructor))
        # Most recent first
        self.assertEqual(bonuses[0], bonus3)
        self.assertEqual(bonuses[1], bonus2)
        self.assertEqual(bonuses[2], bonus1)
    
    def test_instructor_bonus_cascade_delete_with_instructor(self):
        """Test bonus is deleted when instructor is deleted"""
        bonus = InstructorBonus.objects.create(
            instructor=self.instructor,
            amount=Decimal('500.00'),
            bonus_date=date.today()
        )
        
        bonus_id = bonus.id
        self.instructor.delete()
        
        with self.assertRaises(InstructorBonus.DoesNotExist):
            InstructorBonus.objects.get(id=bonus_id)
