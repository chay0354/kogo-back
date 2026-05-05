"""
Unit tests for Customers app models.

Tests coverage:
- Family: basic CRUD, branch relationship
- Parent: full_name property, is_primary flag
- Child: status transitions, full_name property, age calculation
- Payment: status workflow, amount calculations
- RecurringPayment: subscription lifecycle
- TranzilaTransaction: transaction data storage
- PaymentDiscountSnapshot: discount tracking
"""
from decimal import Decimal
from datetime import date, timedelta
from django.test import TestCase
from django.utils import timezone

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import (
    Family, Parent, Child, Payment, RecurringPayment,
    TranzilaTransaction, PaymentDiscountSnapshot
)
from apps.customers.financial_models import Discount


class FamilyModelTest(TestCase):
    """Test Family model"""
    
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
    
    def test_create_family(self):
        """Test creating a family"""
        family = Family.objects.create(
            name="משפחה כהן",
            phone="050-1234567",
            branch=self.branch
        )
        
        self.assertIsNotNone(family.id)
        self.assertEqual(family.name, "משפחה כהן")
        self.assertEqual(family.phone, "050-1234567")
        self.assertEqual(family.branch, self.branch)
    
    def test_family_with_optional_fields(self):
        """Test family with email, address, notes"""
        family = Family.objects.create(
            name="משפחה לוי",
            phone="050-1111111",
            email="levy@example.com",
            address="רחוב הרצל 10, תל אביב",
            parent_id_number="123456789",
            notes="משפחה חדשה",
            branch=self.branch
        )
        
        self.assertEqual(family.email, "levy@example.com")
        self.assertEqual(family.address, "רחוב הרצל 10, תל אביב")
        self.assertEqual(family.parent_id_number, "123456789")
        self.assertEqual(family.notes, "משפחה חדשה")
    
    def test_family_str_representation(self):
        """Test family string representation"""
        family = Family.objects.create(
            name="משפחה דוד",
            phone="050-2222222",
            branch=self.branch
        )
        
        self.assertEqual(str(family), "משפחה דוד")
    
    def test_family_branch_set_null_on_delete(self):
        """Test family.branch is set to null when branch is deleted"""
        family = Family.objects.create(
            name="משפחה יתומה",
            phone="050-3333333",
            branch=self.branch
        )
        
        self.branch.delete()
        family.refresh_from_db()
        
        self.assertIsNone(family.branch)
    
    def test_family_ordering(self):
        """Test families are ordered by name"""
        Family.objects.create(name="זבוטינסקי", phone="050-1", branch=self.branch)
        Family.objects.create(name="אבוטבול", phone="050-2", branch=self.branch)
        Family.objects.create(name="נתניהו", phone="050-3", branch=self.branch)
        
        families = list(Family.objects.all())
        self.assertEqual(families[0].name, "אבוטבול")
        self.assertEqual(families[1].name, "זבוטינסקי")
        self.assertEqual(families[2].name, "נתניהו")


class ParentModelTest(TestCase):
    """Test Parent model"""
    
    def setUp(self):
        self.family = TestDataFactory.create_family()
    
    def test_create_parent(self):
        """Test creating a parent"""
        parent = Parent.objects.create(
            family=self.family,
            first_name="יוסי",
            last_name="כהן",
            phone="050-1234567",
            is_primary=True
        )
        
        self.assertEqual(parent.first_name, "יוסי")
        self.assertEqual(parent.last_name, "כהן")
        self.assertTrue(parent.is_primary)
    
    def test_parent_full_name_property(self):
        """Test parent full_name property"""
        parent = Parent.objects.create(
            family=self.family,
            first_name="דני",
            last_name="לוי",
            phone="050-1111111"
        )
        
        self.assertEqual(parent.full_name, "דני לוי")
    
    def test_parent_str_representation(self):
        """Test parent string representation"""
        parent = Parent.objects.create(
            family=self.family,
            first_name="משה",
            last_name="דהן",
            phone="050-2222222"
        )
        
        self.assertEqual(str(parent), "משה דהן")
    
    def test_parent_is_primary_flag(self):
        """Test parent is_primary flag defaults to False"""
        parent = Parent.objects.create(
            family=self.family,
            first_name="שרה",
            last_name="כהן",
            phone="050-3333333"
        )
        
        self.assertFalse(parent.is_primary)
    
    def test_parent_cascade_delete_with_family(self):
        """Test parent is deleted when family is deleted"""
        parent = Parent.objects.create(
            family=self.family,
            first_name="רחל",
            last_name="אבוטבול",
            phone="050-4444444"
        )
        
        parent_id = parent.id
        self.family.delete()
        
        with self.assertRaises(Parent.DoesNotExist):
            Parent.objects.get(id=parent_id)
    
    def test_parent_ordering(self):
        """Test parents are ordered by family, -is_primary, first_name"""
        family2 = TestDataFactory.create_family(name="משפחה 2")
        
        parent1 = Parent.objects.create(
            family=self.family,
            first_name="זאב",
            last_name="כהן",
            phone="050-1",
            is_primary=False
        )
        parent2 = Parent.objects.create(
            family=self.family,
            first_name="אבי",
            last_name="כהן",
            phone="050-2",
            is_primary=True
        )
        parent3 = Parent.objects.create(
            family=family2,
            first_name="דני",
            last_name="לוי",
            phone="050-3",
            is_primary=True
        )
        
        parents = list(Parent.objects.all())
        # Should be ordered by family, then primary first, then by first_name
        # Check that primary parent comes before non-primary in same family
        family1_parents = [p for p in parents if p.family == self.family]
        self.assertEqual(len(family1_parents), 2)
        self.assertTrue(family1_parents[0].is_primary)  # First should be primary
        self.assertFalse(family1_parents[1].is_primary)  # Second should not be primary


class ChildModelTest(TestCase):
    """Test Child model"""
    
    def setUp(self):
        self.family = TestDataFactory.create_family()
    
    def test_create_child(self):
        """Test creating a child"""
        birth_date = date(2015, 5, 15)
        child = Child.objects.create(
            family=self.family,
            first_name="דני",
            last_name="כהן",
            birth_date=birth_date,
            gender='male'
        )
        
        self.assertEqual(child.first_name, "דני")
        self.assertEqual(child.birth_date, birth_date)
        self.assertEqual(child.gender, 'male')
        self.assertEqual(child.status, 'pending')  # Default status
    
    def test_child_full_name_property(self):
        """Test child full_name property"""
        child = Child.objects.create(
            family=self.family,
            first_name="נועה",
            last_name="לוי",
            birth_date=date(2014, 3, 20),
            gender='female'
        )
        
        self.assertEqual(child.full_name, "נועה לוי")
    
    def test_child_age_property(self):
        """Test child age property calculation"""
        # Child born exactly 8 years ago
        today = date.today()
        birth_date = date(today.year - 8, today.month, today.day)
        child = Child.objects.create(
            family=self.family,
            first_name="יוסי",
            last_name="כהן",
            birth_date=birth_date,
            gender='male'
        )
        
        self.assertEqual(child.age, 8)
    
    def test_child_status_choices(self):
        """Test child can be created with different status values"""
        statuses = ['active', 'trial_signed', 'trial_completed', 'payment_problem', 
                   'not_paid', 'pending', 'ghost', 'inactive']
        
        for status in statuses:
            child = Child.objects.create(
                family=self.family,
                first_name=f"Child-{status}",
                last_name="Test",
                birth_date=date(2015, 1, 1),
                gender='male',
                status=status
            )
            self.assertEqual(child.status, status)
    
    def test_child_status_transition(self):
        """Test child status can be updated"""
        child = Child.objects.create(
            family=self.family,
            first_name="דני",
            last_name="כהן",
            birth_date=date(2015, 1, 1),
            gender='male',
            status='pending'
        )
        
        child.status = 'active'
        child.save()
        child.refresh_from_db()
        
        self.assertEqual(child.status, 'active')
    
    def test_child_absent_irregularly_flag(self):
        """Test child absent_irregularly flag"""
        child = Child.objects.create(
            family=self.family,
            first_name="מיכל",
            last_name="לוי",
            birth_date=date(2016, 6, 10),
            gender='female',
            absent_irregularly=True
        )
        
        self.assertTrue(child.absent_irregularly)
    
    def test_child_paid_until_date(self):
        """Test child paid_until_date tracking"""
        child = Child.objects.create(
            family=self.family,
            first_name="רון",
            last_name="כהן",
            birth_date=date(2015, 8, 20),
            gender='male',
            paid_until_date=date.today() + timedelta(days=30)
        )
        
        self.assertIsNotNone(child.paid_until_date)
    
    def test_child_trial_classes_attended(self):
        """Test child trial_classes_attended counter"""
        child = Child.objects.create(
            family=self.family,
            first_name="שירה",
            last_name="דהן",
            birth_date=date(2017, 2, 14),
            gender='female',
            trial_classes_attended=2
        )
        
        self.assertEqual(child.trial_classes_attended, 2)
    
    def test_child_str_representation(self):
        """Test child string representation"""
        child = Child.objects.create(
            family=self.family,
            first_name="אור",
            last_name="אבוטבול",
            birth_date=date(2015, 11, 5),
            gender='male'
        )
        
        self.assertEqual(str(child), "אור אבוטבול")
    
    def test_child_gender_choices(self):
        """Test child gender choices"""
        male_child = Child.objects.create(
            family=self.family,
            first_name="בן",
            last_name="כהן",
            birth_date=date(2015, 1, 1),
            gender='male'
        )
        
        female_child = Child.objects.create(
            family=self.family,
            first_name="בת",
            last_name="כהן",
            birth_date=date(2016, 1, 1),
            gender='female'
        )
        
        self.assertEqual(male_child.gender, 'male')
        self.assertEqual(female_child.gender, 'female')


class PaymentModelTest(TestCase):
    """Test Payment model"""
    
    def setUp(self):
        self.family = TestDataFactory.create_family()
        self.parent = TestDataFactory.create_parent(family=self.family)
        self.child = TestDataFactory.create_child(family=self.family)
        self.lesson = TestDataFactory.create_lesson()
    
    def test_create_payment(self):
        """Test creating a payment"""
        payment = Payment.objects.create(
            child=self.child,
            family=self.family,
            parent=self.parent,
            branch=self.lesson.branch,
            lesson=self.lesson,
            payment_type='recurring_subscription',
            status='pending',
            base_amount=Decimal('350.00'),
            discount_amount=Decimal('50.00'),
            final_amount=Decimal('300.00'),
            description="מנוי חודשי"
        )
        
        self.assertEqual(payment.payment_type, 'recurring_subscription')
        self.assertEqual(payment.status, 'pending')
        self.assertEqual(payment.base_amount, Decimal('350.00'))
        self.assertEqual(payment.discount_amount, Decimal('50.00'))
        self.assertEqual(payment.final_amount, Decimal('300.00'))
    
    def test_payment_status_workflow(self):
        """Test payment status transitions"""
        payment = Payment.objects.create(
            child=self.child,
            family=self.family,
            payment_type='one_time',
            status='pending',
            base_amount=Decimal('400.00'),
            final_amount=Decimal('400.00')
        )
        
        # Transition to completed
        payment.status = 'completed'
        payment.save()
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'completed')
        
        # Can also transition to failed
        payment.status = 'failed'
        payment.save()
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'failed')
    
    def test_payment_amount_calculation(self):
        """Test payment amount calculation (base - discount = final)"""
        payment = Payment.objects.create(
            child=self.child,
            family=self.family,
            payment_type='one_time',
            status='pending',
            base_amount=Decimal('500.00'),
            discount_amount=Decimal('100.00'),
            final_amount=Decimal('400.00')
        )
        
        # Verify the relationship holds
        expected_final = payment.base_amount - payment.discount_amount
        self.assertEqual(payment.final_amount, expected_final)
    
    def test_payment_with_failure_tracking(self):
        """Test payment failure tracking"""
        payment = Payment.objects.create(
            child=self.child,
            family=self.family,
            payment_type='one_time',
            status='failed',
            base_amount=Decimal('350.00'),
            final_amount=Decimal('350.00'),
            failure_reason="כרטיס אשראי נדחה",
            failure_code="033"
        )
        
        self.assertEqual(payment.failure_reason, "כרטיס אשראי נדחה")
        self.assertEqual(payment.failure_code, "033")
    
    def test_payment_str_representation(self):
        """Test payment string representation"""
        payment = Payment.objects.create(
            child=self.child,
            family=self.family,
            payment_type='one_time',
            status='completed',
            base_amount=Decimal('350.00'),
            final_amount=Decimal('350.00')
        )
        
        str_repr = str(payment)
        self.assertIn(self.child.full_name, str_repr)
        self.assertIn('350', str_repr)
    
    def test_payment_cascade_delete_with_child(self):
        """Test payment is deleted when child is deleted"""
        payment = Payment.objects.create(
            child=self.child,
            family=self.family,
            payment_type='one_time',
            status='completed',
            base_amount=Decimal('350.00'),
            final_amount=Decimal('350.00')
        )
        
        payment_id = payment.id
        self.child.delete()
        
        with self.assertRaises(Payment.DoesNotExist):
            Payment.objects.get(id=payment_id)
    
    def test_payment_default_discount_amount(self):
        """Test payment discount_amount defaults to 0"""
        payment = Payment.objects.create(
            child=self.child,
            family=self.family,
            payment_type='one_time',
            status='pending',
            base_amount=Decimal('350.00'),
            final_amount=Decimal('350.00')
        )
        
        self.assertEqual(payment.discount_amount, Decimal('0.00'))


class RecurringPaymentModelTest(TestCase):
    """Test RecurringPayment model"""
    
    def setUp(self):
        self.child = TestDataFactory.create_child()
    
    def test_create_recurring_payment(self):
        """Test creating a recurring payment"""
        recurring = RecurringPayment.objects.create(
            child=self.child,
            tranzila_token="test_token_123",
            tranzila_recurring_index="1",
            status='active',
            base_amount=Decimal('350.00'),
            discount_amount=Decimal('50.00'),
            amount=Decimal('300.00'),
            billing_day=1,
            start_date=date.today()
        )
        
        self.assertEqual(recurring.tranzila_token, "test_token_123")
        self.assertEqual(recurring.status, 'active')
        self.assertEqual(recurring.amount, Decimal('300.00'))
    
    def test_recurring_payment_status_choices(self):
        """Test recurring payment status choices"""
        statuses = ['active', 'paused', 'cancelled', 'expired', 'failed']
        
        for status in statuses:
            recurring = RecurringPayment.objects.create(
                child=self.child,
                status=status,
                amount=Decimal('350.00'),
                billing_day=1,
                start_date=date.today()
            )
            self.assertEqual(recurring.status, status)
    
    def test_recurring_payment_with_card_expiry(self):
        """Test recurring payment with card expiration tracking"""
        recurring = RecurringPayment.objects.create(
            child=self.child,
            tranzila_token="token_456",
            status='active',
            amount=Decimal('350.00'),
            billing_day=5,
            start_date=date.today(),
            card_expire_month=12,
            card_expire_year=2027
        )
        
        self.assertEqual(recurring.card_expire_month, 12)
        self.assertEqual(recurring.card_expire_year, 2027)
    
    def test_recurring_payment_discount_details_json(self):
        """Test recurring payment discount_details JSON field"""
        discount_info = [
            {'name': 'הנחת ילד שני', 'type': 'second_child', 'value': 50.00},
            {'name': 'הנחת רישום מוקדם', 'type': 'early_signup', 'value': 25.00}
        ]
        
        recurring = RecurringPayment.objects.create(
            child=self.child,
            status='active',
            amount=Decimal('275.00'),
            billing_day=1,
            start_date=date.today(),
            discount_details=discount_info
        )
        
        self.assertEqual(len(recurring.discount_details), 2)
        self.assertEqual(recurring.discount_details[0]['name'], 'הנחת ילד שני')
    
    def test_recurring_payment_next_billing_date(self):
        """Test recurring payment next_billing_date tracking"""
        next_billing = date.today() + timedelta(days=30)
        
        recurring = RecurringPayment.objects.create(
            child=self.child,
            status='active',
            amount=Decimal('350.00'),
            billing_day=1,
            start_date=date.today(),
            next_billing_date=next_billing
        )
        
        self.assertEqual(recurring.next_billing_date, next_billing)
    
    def test_recurring_payment_cancellation_tracking(self):
        """Test recurring payment cancellation tracking"""
        cancelled_time = timezone.now()
        
        recurring = RecurringPayment.objects.create(
            child=self.child,
            status='cancelled',
            amount=Decimal('350.00'),
            billing_day=1,
            start_date=date.today(),
            cancelled_at=cancelled_time,
            cancellation_reason="לבקשת ההורה"
        )
        
        self.assertEqual(recurring.status, 'cancelled')
        self.assertEqual(recurring.cancellation_reason, "לבקשת ההורה")
        self.assertIsNotNone(recurring.cancelled_at)
    
    def test_recurring_payment_str_representation(self):
        """Test recurring payment string representation"""
        recurring = RecurringPayment.objects.create(
            child=self.child,
            status='active',
            amount=Decimal('350.00'),
            billing_day=1,
            start_date=date.today()
        )
        
        str_repr = str(recurring)
        self.assertIn(self.child.full_name, str_repr)
        self.assertIn('350', str_repr)
    
    def test_recurring_payment_cascade_delete_with_child(self):
        """Test recurring payment is deleted when child is deleted"""
        recurring = RecurringPayment.objects.create(
            child=self.child,
            status='active',
            amount=Decimal('350.00'),
            billing_day=1,
            start_date=date.today()
        )
        
        recurring_id = recurring.id
        self.child.delete()
        
        with self.assertRaises(RecurringPayment.DoesNotExist):
            RecurringPayment.objects.get(id=recurring_id)


class TranzilaTransactionModelTest(TestCase):
    """Test TranzilaTransaction model"""
    
    def test_create_tranzila_transaction(self):
        """Test creating a Tranzila transaction"""
        transaction = TranzilaTransaction.objects.create(
            transaction_id="TRX123456",
            confirmation_code="ABC123",
            transaction_type='charge',
            response_code='000',
            response_message='Success',
            is_successful=True,
            idempotency_key="unique_key_123"
        )
        
        self.assertEqual(transaction.transaction_id, "TRX123456")
        self.assertEqual(transaction.response_code, '000')
        self.assertTrue(transaction.is_successful)
    
    def test_tranzila_transaction_with_request_response_data(self):
        """Test Tranzila transaction with JSON request/response data"""
        request_data = {
            'sum': '350.00',
            'currency': '1',
            'pdesc': 'Test payment'
        }
        
        response_data = {
            'Response': '000',
            'ConfirmationCode': 'ABC123',
            'TranzilaTK': 'token_123'
        }
        
        transaction = TranzilaTransaction.objects.create(
            transaction_id="TRX789",
            transaction_type='recurring_setup',
            response_code='000',
            is_successful=True,
            idempotency_key="key_789",
            request_data=request_data,
            response_data=response_data
        )
        
        self.assertEqual(transaction.request_data['sum'], '350.00')
        self.assertEqual(transaction.response_data['Response'], '000')
    
    def test_tranzila_transaction_type_choices(self):
        """Test Tranzila transaction type choices"""
        types = ['authorization', 'charge', 'refund', 'recurring_setup', 'recurring_charge']
        
        for idx, trans_type in enumerate(types):
            transaction = TranzilaTransaction.objects.create(
                transaction_id=f"TRX{idx}",
                transaction_type=trans_type,
                is_successful=True,
                idempotency_key=f"key_{idx}"
            )
            self.assertEqual(transaction.transaction_type, trans_type)
    
    def test_tranzila_transaction_idempotency_key_unique(self):
        """Test Tranzila transaction idempotency_key is unique"""
        TranzilaTransaction.objects.create(
            transaction_id="TRX1",
            transaction_type='charge',
            is_successful=True,
            idempotency_key="duplicate_key"
        )
        
        # Creating another with same idempotency_key should raise error
        with self.assertRaises(Exception):  # IntegrityError
            TranzilaTransaction.objects.create(
                transaction_id="TRX2",
                transaction_type='charge',
                is_successful=True,
                idempotency_key="duplicate_key"
            )
    
    def test_tranzila_transaction_str_representation(self):
        """Test Tranzila transaction string representation"""
        transaction = TranzilaTransaction.objects.create(
            transaction_id="TRX999",
            transaction_type='charge',
            is_successful=True,
            idempotency_key="key_999"
        )
        
        self.assertIn("TRX999", str(transaction))
    
    def test_tranzila_transaction_timestamps(self):
        """Test Tranzila transaction timestamp tracking"""
        request_time = timezone.now()
        response_time = timezone.now() + timedelta(seconds=2)
        
        transaction = TranzilaTransaction.objects.create(
            transaction_id="TRX_TIME",
            transaction_type='charge',
            is_successful=True,
            idempotency_key="key_time",
            request_timestamp=request_time,
            response_timestamp=response_time
        )
        
        self.assertIsNotNone(transaction.request_timestamp)
        self.assertIsNotNone(transaction.response_timestamp)


class PaymentDiscountSnapshotModelTest(TestCase):
    """Test PaymentDiscountSnapshot model"""
    
    def setUp(self):
        self.child = TestDataFactory.create_child()
        self.payment = Payment.objects.create(
            child=self.child,
            family=self.child.family,
            payment_type='one_time',
            status='completed',
            base_amount=Decimal('350.00'),
            discount_amount=Decimal('50.00'),
            final_amount=Decimal('300.00')
        )
    
    def test_create_payment_discount_snapshot(self):
        """Test creating a payment discount snapshot"""
        snapshot = PaymentDiscountSnapshot.objects.create(
            payment=self.payment,
            discount_name="הנחת ילד שני",
            discount_type="second_child",
            discount_value=Decimal('50.00'),
            amount_deducted=Decimal('50.00'),
            reason="ילד שני במשפחה"
        )
        
        self.assertEqual(snapshot.discount_name, "הנחת ילד שני")
        self.assertEqual(snapshot.discount_type, "second_child")
        self.assertEqual(snapshot.amount_deducted, Decimal('50.00'))
    
    def test_payment_discount_snapshot_with_discount_fk(self):
        """Test payment discount snapshot with FK to Discount model"""
        discount = Discount.objects.create(
            name="הנחת רישום מוקדם",
            discount_type="fixed",
            value=Decimal('75.00'),
            applies_to='child',
            promotion_type='temporary',
            is_active=True,
            start_date=date.today(),
            end_date=date.today() + timedelta(days=30)
        )
        
        snapshot = PaymentDiscountSnapshot.objects.create(
            payment=self.payment,
            discount=discount,
            discount_name=discount.name,
            discount_type=discount.discount_type,
            discount_value=discount.value,
            amount_deducted=Decimal('75.00')
        )
        
        self.assertEqual(snapshot.discount, discount)
    
    def test_payment_discount_snapshot_cascade_delete_with_payment(self):
        """Test snapshot is deleted when payment is deleted"""
        snapshot = PaymentDiscountSnapshot.objects.create(
            payment=self.payment,
            discount_name="הנחה",
            discount_type="test",
            discount_value=Decimal('50.00'),
            amount_deducted=Decimal('50.00')
        )
        
        snapshot_id = snapshot.id
        self.payment.delete()
        
        with self.assertRaises(PaymentDiscountSnapshot.DoesNotExist):
            PaymentDiscountSnapshot.objects.get(id=snapshot_id)
    
    def test_payment_multiple_discount_snapshots(self):
        """Test payment can have multiple discount snapshots"""
        snapshot1 = PaymentDiscountSnapshot.objects.create(
            payment=self.payment,
            discount_name="הנחת ילד שני",
            discount_type="second_child",
            discount_value=Decimal('30.00'),
            amount_deducted=Decimal('30.00')
        )
        
        snapshot2 = PaymentDiscountSnapshot.objects.create(
            payment=self.payment,
            discount_name="הנחת רישום מוקדם",
            discount_type="early_signup",
            discount_value=Decimal('20.00'),
            amount_deducted=Decimal('20.00')
        )
        
        snapshots = self.payment.discount_snapshots.all()
        self.assertEqual(snapshots.count(), 2)
        self.assertIn(snapshot1, snapshots)
        self.assertIn(snapshot2, snapshots)
