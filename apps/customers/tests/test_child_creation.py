"""
Tests for Child and Family Creation with ID Numbers

Tests the logic used in the Elementor widget subscription flow and 
"לקוח חדש" functionality.
"""
from django.test import TestCase
from apps.customers.models import Family, Parent, Child
from apps.customers.serializers import ChildUpdateSerializer
from .test_fixtures import create_test_family, create_test_child, create_test_branch, create_test_parent
from datetime import date


class ChildFamilyCreationTests(TestCase):
    """Test child and family creation with ID number logic"""
    
    def setUp(self):
        """Set up test data"""
        self.branch = create_test_branch()
    
    def test_create_child_with_new_family(self):
        """
        Test: Create child with brand new family
        
        Scenario:
        - No existing family with this parent_id_number
        - Create family, parent, and child
        
        Expected:
        - Family created with parent_id_number
        - Parent created linked to family
        - Child created linked to family
        """
        # Create family
        family = Family.objects.create(
            name="Smith",
            phone="050-1111111",
            email="smith@test.com",
            parent_id_number="123456789",
            branch=self.branch
        )
        
        # Create parent
        parent = Parent.objects.create(
            family=family,
            first_name="John",
            last_name="Smith",
            phone="050-1111111",
            is_primary=True
        )
        
        # Create child
        child = Child.objects.create(
            family=family,
            first_name="Alice",
            last_name="Smith",
            id_number="987654321",
            birth_date=date(2015, 5, 15),
            gender="female",
            status="trial"
        )
        
        # Verify family created correctly
        self.assertEqual(family.parent_id_number, "123456789")
        self.assertEqual(family.name, "Smith")
        
        # Verify parent linked to family
        self.assertEqual(parent.family, family)
        self.assertTrue(parent.is_primary)
        
        # Verify child linked to family
        self.assertEqual(child.family, family)
        self.assertEqual(child.id_number, "987654321")
        
        # Verify relationships
        self.assertEqual(family.parents.count(), 1)
        self.assertEqual(family.children.count(), 1)
    
    def test_create_child_existing_family_by_parent_id(self):
        """
        Test: Add child to existing family using parent_id_number
        
        Scenario:
        - Family already exists with parent_id_number "123456789"
        - New child with same parent_id_number
        
        Expected:
        - No new family created
        - Child added to existing family
        - Family now has 2 children
        """
        # Create existing family
        existing_family = Family.objects.create(
            name="Johnson",
            phone="050-2222222",
            parent_id_number="111222333",
            branch=self.branch
        )
        
        # Create first child
        child1 = Child.objects.create(
            family=existing_family,
            first_name="Bob",
            last_name="Johnson",
            id_number="111111111",
            birth_date=date(2016, 3, 10),
            gender="male",
            status="trial"
        )
        
        # Find existing family by parent_id_number (simulating widget logic)
        family = Family.objects.filter(parent_id_number="111222333").first()
        self.assertIsNotNone(family)
        self.assertEqual(family.id, existing_family.id)
        
        # Create second child in same family
        child2 = Child.objects.create(
            family=family,
            first_name="Sarah",
            last_name="Johnson",
            id_number="222222222",
            birth_date=date(2018, 7, 20),
            gender="female",
            status="trial"
        )
        
        # Verify no duplicate family created
        self.assertEqual(Family.objects.filter(parent_id_number="111222333").count(), 1)
        
        # Verify both children in same family
        self.assertEqual(child1.family, child2.family)
        self.assertEqual(family.children.count(), 2)
    
    def test_create_child_duplicate_id_number(self):
        """
        Test: Child with duplicate id_number should update existing child
        
        Scenario:
        - Child exists with id_number "987654321"
        - Attempt to create/update child with same id_number
        
        Expected:
        - No duplicate child created
        - Existing child's data can be updated
        """
        family1 = create_test_family(name="Family One", parent_id="111111111")
        
        # Create first child
        child = Child.objects.create(
            family=family1,
            first_name="Original",
            last_name="Name",
            id_number="555555555",
            birth_date=date(2015, 1, 1),
            gender="male",
            status="trial"
        )
        
        original_id = child.id
        
        # Simulate widget finding existing child by id_number
        existing_child = Child.objects.filter(id_number="555555555").first()
        self.assertIsNotNone(existing_child)
        self.assertEqual(existing_child.id, original_id)
        
        # Update existing child (as done in widget subscribe logic)
        existing_child.first_name = "Updated"
        existing_child.last_name = "Name"
        existing_child.save()
        
        # Verify no duplicate created
        self.assertEqual(Child.objects.filter(id_number="555555555").count(), 1)
        
        # Verify child was updated
        updated_child = Child.objects.get(id=original_id)
        self.assertEqual(updated_child.first_name, "Updated")
    
    def test_child_update_paid_until_date(self):
        """
        Test: ChildUpdateSerializer includes paid_until_date field
        
        Scenario:
        - Child exists
        - PATCH request to update paid_until_date
        
        Expected:
        - paid_until_date field is in serializer fields
        - Update succeeds
        
        This verifies the bug fix where paid_until_date was missing from serializer.
        """
        family = create_test_family()
        child = create_test_child(family=family)
        
        # Verify ChildUpdateSerializer includes paid_until_date
        serializer_fields = ChildUpdateSerializer.Meta.fields
        self.assertIn('paid_until_date', serializer_fields)
        self.assertIn('subscription_start_date', serializer_fields)
        self.assertIn('subscription_end_date', serializer_fields)
        
        # Test update
        update_data = {
            'paid_until_date': date.today(),
            'subscription_start_date': date.today(),
            'subscription_end_date': date(2026, 12, 31)
        }
        
        serializer = ChildUpdateSerializer(child, data=update_data, partial=True)
        self.assertTrue(serializer.is_valid())
        
        updated_child = serializer.save()
        self.assertEqual(updated_child.paid_until_date, update_data['paid_until_date'])
        self.assertEqual(updated_child.subscription_start_date, update_data['subscription_start_date'])


class FamilyUniquenessTests(TestCase):
    """Test family uniqueness by parent ID number"""
    
    def setUp(self):
        """Set up test data"""
        self.branch = create_test_branch()
    
    def test_find_family_by_parent_id_number(self):
        """
        Test: Can find existing family by parent_id_number
        
        Scenario:
        - Multiple families exist
        - Search by parent_id_number
        
        Expected:
        - Returns correct family
        """
        family1 = Family.objects.create(
            name="Family A",
            phone="050-1111111",
            parent_id_number="AAAA",
            branch=self.branch
        )
        
        family2 = Family.objects.create(
            name="Family B",
            phone="050-2222222",
            parent_id_number="BBBB",
            branch=self.branch
        )
        
        # Find by parent_id_number
        found = Family.objects.filter(parent_id_number="BBBB").first()
        self.assertEqual(found.id, family2.id)
        self.assertEqual(found.name, "Family B")
    
    def test_parent_id_number_can_be_blank(self):
        """
        Test: parent_id_number is optional (can be blank)
        
        Scenario:
        - Create family without parent_id_number
        
        Expected:
        - Family created successfully
        - Can still create children
        """
        family = Family.objects.create(
            name="No ID Family",
            phone="050-3333333",
            parent_id_number="",  # Blank
            branch=self.branch
        )
        
        self.assertEqual(family.parent_id_number, "")
        
        # Can still create child
        child = Child.objects.create(
            family=family,
            first_name="Test",
            last_name="Child",
            id_number="999999999",
            birth_date=date(2015, 1, 1),
            gender="male",
            status="trial"
        )
        
        self.assertEqual(child.family, family)

