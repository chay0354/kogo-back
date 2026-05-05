"""
Unit tests for Financial models.

Tests coverage:
- Invoice: creation, child associations through InvoiceChild
- InvoiceChild: M2M relationship between Invoice and Child
- InvoiceActivityLog: audit trail creation
- Discount: date range validation, discount type choices
- BranchDiscountMetrics: aggregation of discount usage
"""
from decimal import Decimal
from datetime import date, timedelta
from django.test import TestCase
from django.utils import timezone

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Family, Parent, Child, Payment
from apps.customers.financial_models import (
    Invoice, InvoiceChild, InvoiceActivityLog, Discount, BranchDiscountMetrics
)


class InvoiceModelTest(TestCase):
    """Test Invoice model"""
    
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
        self.family = TestDataFactory.create_family(branch=self.branch)
        self.parent = TestDataFactory.create_parent(family=self.family)
    
    def test_create_invoice(self):
        """Test creating an invoice"""
        invoice = Invoice.objects.create(
            invoice_number="INV-2024-001",
            family=self.family,
            parent=self.parent,
            branch=self.branch,
            amount=Decimal('350.00'),
            status='pending',
            payment_method='credit_card',
            payment_type='recurring',
            invoice_date=timezone.now()
        )
        
        self.assertEqual(invoice.invoice_number, "INV-2024-001")
        self.assertEqual(invoice.amount, Decimal('350.00'))
        self.assertEqual(invoice.status, 'pending')
        self.assertEqual(invoice.payment_method, 'credit_card')
    
    def test_invoice_status_choices(self):
        """Test invoice status choices"""
        statuses = ['pending', 'paid', 'failed', 'cancelled', 'credit']
        
        for status in statuses:
            invoice = Invoice.objects.create(
                invoice_number=f"INV-{status}",
                family=self.family,
                amount=Decimal('100.00'),
                status=status,
                invoice_date=timezone.now()
            )
            self.assertEqual(invoice.status, status)
    
    def test_invoice_payment_method_choices(self):
        """Test invoice payment method choices"""
        methods = ['credit_card', 'cash', 'bank_transfer', 'check']
        
        for idx, method in enumerate(methods):
            invoice = Invoice.objects.create(
                invoice_number=f"INV-{idx}",
                family=self.family,
                amount=Decimal('100.00'),
                payment_method=method,
                invoice_date=timezone.now()
            )
            self.assertEqual(invoice.payment_method, method)
    
    def test_invoice_payment_type_choices(self):
        """Test invoice payment type choices"""
        types = ['recurring', 'one_time', 'manual']
        
        for payment_type in types:
            invoice = Invoice.objects.create(
                invoice_number=f"INV-{payment_type}",
                family=self.family,
                amount=Decimal('100.00'),
                payment_type=payment_type,
                invoice_date=timezone.now()
            )
            self.assertEqual(invoice.payment_type, payment_type)
    
    def test_invoice_with_payment_link(self):
        """Test invoice with link to Payment model"""
        child = TestDataFactory.create_child(family=self.family)
        payment = Payment.objects.create(
            child=child,
            family=self.family,
            parent=self.parent,
            payment_type='one_time',
            status='completed',
            base_amount=Decimal('350.00'),
            final_amount=Decimal('350.00')
        )
        
        invoice = Invoice.objects.create(
            invoice_number="INV-LINK-001",
            family=self.family,
            parent=self.parent,
            branch=self.branch,
            payment=payment,
            amount=Decimal('350.00'),
            status='paid',
            invoice_date=timezone.now()
        )
        
        self.assertEqual(invoice.payment, payment)
    
    def test_invoice_with_external_ids(self):
        """Test invoice with external payment system IDs"""
        invoice = Invoice.objects.create(
            invoice_number="INV-EXT-001",
            family=self.family,
            amount=Decimal('350.00'),
            meshulam_id="MSH123456",
            tranzila_transaction_id="TRX789012",
            external_transaction_id="EXT999888",
            invoice_date=timezone.now()
        )
        
        self.assertEqual(invoice.meshulam_id, "MSH123456")
        self.assertEqual(invoice.tranzila_transaction_id, "TRX789012")
        self.assertEqual(invoice.external_transaction_id, "EXT999888")
    
    def test_invoice_communication_tracking(self):
        """Test invoice email and WhatsApp sent tracking"""
        invoice = Invoice.objects.create(
            invoice_number="INV-COM-001",
            family=self.family,
            amount=Decimal('350.00'),
            invoice_date=timezone.now(),
            email_sent_at=timezone.now(),
            whatsapp_sent_at=timezone.now()
        )
        
        self.assertIsNotNone(invoice.email_sent_at)
        self.assertIsNotNone(invoice.whatsapp_sent_at)
    
    def test_invoice_str_representation(self):
        """Test invoice string representation"""
        invoice = Invoice.objects.create(
            invoice_number="INV-STR-001",
            family=self.family,
            amount=Decimal('350.00'),
            invoice_date=timezone.now()
        )
        
        str_repr = str(invoice)
        self.assertIn("INV-STR-001", str_repr)
        self.assertIn(self.family.name, str_repr)
    
    def test_invoice_unique_invoice_number(self):
        """Test invoice number must be unique"""
        Invoice.objects.create(
            invoice_number="INV-UNIQUE",
            family=self.family,
            amount=Decimal('100.00'),
            invoice_date=timezone.now()
        )
        
        # Creating another with same invoice_number should raise error
        with self.assertRaises(Exception):  # IntegrityError
            Invoice.objects.create(
                invoice_number="INV-UNIQUE",
                family=self.family,
                amount=Decimal('200.00'),
                invoice_date=timezone.now()
            )
    
    def test_invoice_cascade_delete_with_family(self):
        """Test invoice is deleted when family is deleted"""
        invoice = Invoice.objects.create(
            invoice_number="INV-CASCADE",
            family=self.family,
            amount=Decimal('350.00'),
            invoice_date=timezone.now()
        )
        
        invoice_id = invoice.id
        self.family.delete()
        
        with self.assertRaises(Invoice.DoesNotExist):
            Invoice.objects.get(id=invoice_id)


class InvoiceChildModelTest(TestCase):
    """Test InvoiceChild model"""
    
    def setUp(self):
        self.family = TestDataFactory.create_family()
        self.child = TestDataFactory.create_child(family=self.family)
        self.invoice = Invoice.objects.create(
            invoice_number="INV-CHILD-001",
            family=self.family,
            amount=Decimal('350.00'),
            invoice_date=timezone.now()
        )
        self.course = TestDataFactory.create_course()
        self.lesson = TestDataFactory.create_lesson(course=self.course)
    
    def test_create_invoice_child(self):
        """Test creating an invoice-child relationship"""
        invoice_child = InvoiceChild.objects.create(
            invoice=self.invoice,
            child=self.child,
            course=self.course
        )
        
        self.assertEqual(invoice_child.invoice, self.invoice)
        self.assertEqual(invoice_child.child, self.child)
        self.assertEqual(invoice_child.course, self.course)
    
    def test_invoice_child_with_lesson(self):
        """Test invoice child with specific lesson"""
        invoice_child = InvoiceChild.objects.create(
            invoice=self.invoice,
            child=self.child,
            course=self.course,
            lesson=self.lesson
        )
        
        self.assertEqual(invoice_child.lesson, self.lesson)
    
    def test_invoice_multiple_children(self):
        """Test invoice can have multiple children"""
        child2 = TestDataFactory.create_child(family=self.family, first_name="שרה")
        
        invoice_child1 = InvoiceChild.objects.create(
            invoice=self.invoice,
            child=self.child,
            course=self.course
        )
        
        invoice_child2 = InvoiceChild.objects.create(
            invoice=self.invoice,
            child=child2,
            course=self.course
        )
        
        invoice_children = self.invoice.children.all()
        self.assertEqual(invoice_children.count(), 2)
        self.assertIn(invoice_child1, invoice_children)
        self.assertIn(invoice_child2, invoice_children)
    
    def test_invoice_child_str_representation(self):
        """Test invoice child string representation"""
        invoice_child = InvoiceChild.objects.create(
            invoice=self.invoice,
            child=self.child
        )
        
        str_repr = str(invoice_child)
        self.assertIn(self.child.full_name, str_repr)
        self.assertIn(self.invoice.invoice_number, str_repr)
    
    def test_invoice_child_cascade_delete_with_invoice(self):
        """Test invoice child is deleted when invoice is deleted"""
        invoice_child = InvoiceChild.objects.create(
            invoice=self.invoice,
            child=self.child
        )
        
        invoice_child_id = invoice_child.id
        self.invoice.delete()
        
        with self.assertRaises(InvoiceChild.DoesNotExist):
            InvoiceChild.objects.get(id=invoice_child_id)
    
    def test_invoice_child_cascade_delete_with_child(self):
        """Test invoice child is deleted when child is deleted"""
        invoice_child = InvoiceChild.objects.create(
            invoice=self.invoice,
            child=self.child
        )
        
        invoice_child_id = invoice_child.id
        self.child.delete()
        
        with self.assertRaises(InvoiceChild.DoesNotExist):
            InvoiceChild.objects.get(id=invoice_child_id)


class InvoiceActivityLogModelTest(TestCase):
    """Test InvoiceActivityLog model"""
    
    def setUp(self):
        self.family = TestDataFactory.create_family()
        self.invoice = Invoice.objects.create(
            invoice_number="INV-LOG-001",
            family=self.family,
            amount=Decimal('350.00'),
            invoice_date=timezone.now()
        )
    
    def test_create_invoice_activity_log(self):
        """Test creating an invoice activity log"""
        log = InvoiceActivityLog.objects.create(
            invoice=self.invoice,
            action="created",
            details={'user': 'admin', 'timestamp': '2024-01-15'}
        )
        
        self.assertEqual(log.action, "created")
        self.assertEqual(log.details['user'], 'admin')
    
    def test_invoice_activity_log_with_json_details(self):
        """Test invoice activity log with JSON details"""
        details = {
            'action_type': 'status_change',
            'old_status': 'pending',
            'new_status': 'paid',
            'amount': '350.00'
        }
        
        log = InvoiceActivityLog.objects.create(
            invoice=self.invoice,
            action="status_updated",
            details=details
        )
        
        self.assertEqual(log.details['old_status'], 'pending')
        self.assertEqual(log.details['new_status'], 'paid')
    
    def test_invoice_multiple_activity_logs(self):
        """Test invoice can have multiple activity logs"""
        log1 = InvoiceActivityLog.objects.create(
            invoice=self.invoice,
            action="created"
        )
        
        log2 = InvoiceActivityLog.objects.create(
            invoice=self.invoice,
            action="email_sent"
        )
        
        log3 = InvoiceActivityLog.objects.create(
            invoice=self.invoice,
            action="payment_received"
        )
        
        logs = self.invoice.activity_logs.all()
        self.assertEqual(logs.count(), 3)
    
    def test_invoice_activity_log_str_representation(self):
        """Test invoice activity log string representation"""
        log = InvoiceActivityLog.objects.create(
            invoice=self.invoice,
            action="sent_via_whatsapp"
        )
        
        str_repr = str(log)
        self.assertIn(self.invoice.invoice_number, str_repr)
        self.assertIn("sent_via_whatsapp", str_repr)
    
    def test_invoice_activity_log_cascade_delete_with_invoice(self):
        """Test invoice activity log is deleted when invoice is deleted"""
        log = InvoiceActivityLog.objects.create(
            invoice=self.invoice,
            action="test_action"
        )
        
        log_id = log.id
        self.invoice.delete()
        
        with self.assertRaises(InvoiceActivityLog.DoesNotExist):
            InvoiceActivityLog.objects.get(id=log_id)
    
    def test_invoice_activity_log_ordering(self):
        """Test invoice activity logs are ordered by created_at descending"""
        import time
        
        log1 = InvoiceActivityLog.objects.create(
            invoice=self.invoice,
            action="first"
        )
        time.sleep(0.01)  # Small delay to ensure different timestamps
        
        log2 = InvoiceActivityLog.objects.create(
            invoice=self.invoice,
            action="second"
        )
        time.sleep(0.01)
        
        log3 = InvoiceActivityLog.objects.create(
            invoice=self.invoice,
            action="third"
        )
        
        logs = list(InvoiceActivityLog.objects.filter(invoice=self.invoice))
        # Verify 3 logs were created and they're in reverse chronological order
        self.assertEqual(len(logs), 3)
        # Most recent should be first (log3), oldest should be last (log1)
        self.assertEqual(logs[0].action, "third")
        self.assertEqual(logs[2].action, "first")


class DiscountModelTest(TestCase):
    """Test Discount model"""
    
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
    
    def test_create_discount(self):
        """Test creating a discount"""
        discount = Discount.objects.create(
            name="הנחת ילד שני",
            description="הנחה אוטומטית לילד שני במשפחה",
            discount_type='percentage',
            value=Decimal('10.00'),
            applies_to='child',
            promotion_type='permanent',
            is_active=True
        )
        
        self.assertEqual(discount.name, "הנחת ילד שני")
        self.assertEqual(discount.discount_type, 'percentage')
        self.assertEqual(discount.value, Decimal('10.00'))
    
    def test_discount_type_choices(self):
        """Test discount type choices"""
        types = ['percentage', 'fixed', 'fixed_final_price']
        
        for discount_type in types:
            discount = Discount.objects.create(
                name=f"הנחה {discount_type}",
                discount_type=discount_type,
                value=Decimal('50.00'),
                applies_to='child',
                promotion_type='permanent'
            )
            self.assertEqual(discount.discount_type, discount_type)
    
    def test_discount_applies_to_choices(self):
        """Test discount applies_to choices"""
        applies = ['family', 'child', 'course', 'lesson']
        
        for applies_to in applies:
            discount = Discount.objects.create(
                name=f"הנחה {applies_to}",
                discount_type='fixed',
                value=Decimal('50.00'),
                applies_to=applies_to,
                promotion_type='permanent'
            )
            self.assertEqual(discount.applies_to, applies_to)
    
    def test_discount_promotion_type_choices(self):
        """Test discount promotion type choices"""
        discount_permanent = Discount.objects.create(
            name="הנחה קבועה",
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='permanent'
        )
        
        discount_temporary = Discount.objects.create(
            name="הנחה זמנית",
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='temporary'
        )
        
        self.assertEqual(discount_permanent.promotion_type, 'permanent')
        self.assertEqual(discount_temporary.promotion_type, 'temporary')
    
    def test_discount_with_date_range(self):
        """Test discount with start and end dates"""
        start_date = date.today()
        end_date = date.today() + timedelta(days=30)
        
        discount = Discount.objects.create(
            name="הנחת רישום מוקדם",
            discount_type='fixed',
            value=Decimal('75.00'),
            applies_to='child',
            promotion_type='temporary',
            start_date=start_date,
            end_date=end_date,
            is_active=True
        )
        
        self.assertEqual(discount.start_date, start_date)
        self.assertEqual(discount.end_date, end_date)
    
    def test_discount_is_built_in_flag(self):
        """Test discount is_built_in flag for system discounts"""
        discount = Discount.objects.create(
            name="הנחה מובנית",
            discount_type='percentage',
            value=Decimal('15.00'),
            applies_to='child',
            promotion_type='permanent',
            is_built_in=True
        )
        
        self.assertTrue(discount.is_built_in)
    
    def test_discount_is_active_flag(self):
        """Test discount can be activated/deactivated"""
        discount = Discount.objects.create(
            name="הנחה פעילה",
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='permanent',
            is_active=True
        )
        
        self.assertTrue(discount.is_active)
        
        discount.is_active = False
        discount.save()
        discount.refresh_from_db()
        
        self.assertFalse(discount.is_active)
    
    def test_discount_str_representation(self):
        """Test discount string representation"""
        discount = Discount.objects.create(
            name="הנחת בדיקה",
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='permanent'
        )
        
        self.assertEqual(str(discount), "הנחת בדיקה")
    
    def test_discount_ordering(self):
        """Test discounts are ordered by name"""
        Discount.objects.create(
            name="זהב",
            discount_type='fixed',
            value=Decimal('50.00'),
            applies_to='child',
            promotion_type='permanent'
        )
        
        Discount.objects.create(
            name="אבן",
            discount_type='fixed',
            value=Decimal('25.00'),
            applies_to='child',
            promotion_type='permanent'
        )
        
        Discount.objects.create(
            name="נחושת",
            discount_type='fixed',
            value=Decimal('30.00'),
            applies_to='child',
            promotion_type='permanent'
        )
        
        discounts = list(Discount.objects.all())
        self.assertEqual(discounts[0].name, "אבן")
        self.assertEqual(discounts[1].name, "זהב")
        self.assertEqual(discounts[2].name, "נחושת")


class BranchDiscountMetricsModelTest(TestCase):
    """Test BranchDiscountMetrics model"""
    
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
    
    def test_create_branch_discount_metrics(self):
        """Test creating branch discount metrics"""
        month = date(2024, 1, 1)
        
        metrics = BranchDiscountMetrics.objects.create(
            branch=self.branch,
            month=month,
            total_discount_amount=Decimal('5000.00'),
            discount_count=50,
            early_signup_total=Decimal('2000.00'),
            second_child_total=Decimal('2500.00'),
            fixed_price_total=Decimal('500.00')
        )
        
        self.assertEqual(metrics.total_discount_amount, Decimal('5000.00'))
        self.assertEqual(metrics.discount_count, 50)
        self.assertEqual(metrics.early_signup_total, Decimal('2000.00'))
    
    def test_branch_discount_metrics_breakdown(self):
        """Test branch discount metrics tracks breakdown by type"""
        month = date(2024, 2, 1)
        
        metrics = BranchDiscountMetrics.objects.create(
            branch=self.branch,
            month=month,
            total_discount_amount=Decimal('10000.00'),
            discount_count=100,
            early_signup_total=Decimal('4000.00'),
            second_child_total=Decimal('5000.00'),
            fixed_price_total=Decimal('1000.00')
        )
        
        # Verify breakdown sums up close to total (allowing for rounding)
        total_breakdown = (
            metrics.early_signup_total +
            metrics.second_child_total +
            metrics.fixed_price_total
        )
        self.assertEqual(total_breakdown, Decimal('10000.00'))
    
    def test_branch_discount_metrics_unique_constraint(self):
        """Test branch discount metrics has unique constraint on branch+month"""
        month = date(2024, 3, 1)
        
        BranchDiscountMetrics.objects.create(
            branch=self.branch,
            month=month,
            total_discount_amount=Decimal('1000.00')
        )
        
        # Creating duplicate should raise error
        with self.assertRaises(Exception):  # IntegrityError
            BranchDiscountMetrics.objects.create(
                branch=self.branch,
                month=month,
                total_discount_amount=Decimal('2000.00')
            )
    
    def test_branch_discount_metrics_str_representation(self):
        """Test branch discount metrics string representation"""
        month = date(2024, 4, 1)
        
        metrics = BranchDiscountMetrics.objects.create(
            branch=self.branch,
            month=month,
            total_discount_amount=Decimal('3500.00')
        )
        
        str_repr = str(metrics)
        self.assertIn(self.branch.name, str_repr)
        self.assertIn('2024-04', str_repr)
        self.assertIn('3500', str_repr)
    
    def test_branch_discount_metrics_ordering(self):
        """Test branch discount metrics are ordered by -month, branch"""
        branch2 = TestDataFactory.create_branch(name="סניף 2")
        
        metrics1 = BranchDiscountMetrics.objects.create(
            branch=self.branch,
            month=date(2024, 1, 1),
            total_discount_amount=Decimal('1000.00')
        )
        
        metrics2 = BranchDiscountMetrics.objects.create(
            branch=self.branch,
            month=date(2024, 3, 1),
            total_discount_amount=Decimal('1500.00')
        )
        
        metrics3 = BranchDiscountMetrics.objects.create(
            branch=branch2,
            month=date(2024, 3, 1),
            total_discount_amount=Decimal('2000.00')
        )
        
        metrics_list = list(BranchDiscountMetrics.objects.all())
        # Should be ordered by -month (most recent first), then by branch
        self.assertEqual(metrics_list[0].month, date(2024, 3, 1))
        self.assertEqual(metrics_list[2].month, date(2024, 1, 1))
    
    def test_branch_discount_metrics_cascade_delete_with_branch(self):
        """Test branch discount metrics is deleted when branch is deleted"""
        month = date(2024, 5, 1)
        
        metrics = BranchDiscountMetrics.objects.create(
            branch=self.branch,
            month=month,
            total_discount_amount=Decimal('1000.00')
        )
        
        metrics_id = metrics.id
        self.branch.delete()
        
        with self.assertRaises(BranchDiscountMetrics.DoesNotExist):
            BranchDiscountMetrics.objects.get(id=metrics_id)
