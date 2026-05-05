"""
Unit tests for Status History models.

Tests coverage:
- ChildStatusHistory: status change tracking, automatic timestamp
"""
from django.test import TestCase
from django.utils import timezone
from django.contrib.auth import get_user_model

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.status_history_models import ChildStatusHistory

User = get_user_model()


class ChildStatusHistoryModelTest(TestCase):
    """Test ChildStatusHistory model"""
    
    def setUp(self):
        self.child = TestDataFactory.create_child()
        self.user = User.objects.create_user(
            username='testuser@test.com',
            password='testpass123'
        )
    
    def test_create_status_history(self):
        """Test creating a status history entry"""
        history = ChildStatusHistory.objects.create(
            child=self.child,
            previous_status='pending',
            new_status='active'
        )
        
        self.assertEqual(history.child, self.child)
        self.assertEqual(history.previous_status, 'pending')
        self.assertEqual(history.new_status, 'active')
        self.assertIsNotNone(history.changed_at)
    
    def test_status_history_with_reason(self):
        """Test status history with reason for change"""
        history = ChildStatusHistory.objects.create(
            child=self.child,
            previous_status='active',
            new_status='payment_problem',
            reason='כרטיס אשראי נדחה'
        )
        
        self.assertEqual(history.reason, 'כרטיס אשראי נדחה')
    
    def test_status_history_with_changed_by(self):
        """Test status history tracks who made the change"""
        history = ChildStatusHistory.objects.create(
            child=self.child,
            previous_status='pending',
            new_status='active',
            changed_by=self.user
        )
        
        self.assertEqual(history.changed_by, self.user)
    
    def test_status_history_automatic_timestamp(self):
        """Test status history has automatic timestamp"""
        before = timezone.now()
        
        history = ChildStatusHistory.objects.create(
            child=self.child,
            previous_status='active',
            new_status='inactive'
        )
        
        after = timezone.now()
        
        self.assertGreaterEqual(history.changed_at, before)
        self.assertLessEqual(history.changed_at, after)
    
    def test_status_history_multiple_entries(self):
        """Test child can have multiple status history entries"""
        history1 = ChildStatusHistory.objects.create(
            child=self.child,
            previous_status='pending',
            new_status='trial_signed'
        )
        
        history2 = ChildStatusHistory.objects.create(
            child=self.child,
            previous_status='trial_signed',
            new_status='trial_completed'
        )
        
        history3 = ChildStatusHistory.objects.create(
            child=self.child,
            previous_status='trial_completed',
            new_status='active'
        )
        
        history_entries = self.child.status_history.all()
        self.assertEqual(history_entries.count(), 3)
    
    def test_status_history_str_representation(self):
        """Test status history string representation"""
        history = ChildStatusHistory.objects.create(
            child=self.child,
            previous_status='active',
            new_status='inactive'
        )
        
        str_repr = str(history)
        self.assertIn(self.child.full_name, str_repr)
        self.assertIn('active', str_repr)
        self.assertIn('inactive', str_repr)
    
    def test_status_history_ordering(self):
        """Test status history ordered by -changed_at (most recent first)"""
        from django.utils import timezone
        from datetime import timedelta
        
        history1 = ChildStatusHistory.objects.create(
            child=self.child,
            previous_status='pending',
            new_status='active',
            changed_at=timezone.now() - timedelta(minutes=3)
        )
        
        history2 = ChildStatusHistory.objects.create(
            child=self.child,
            previous_status='active',
            new_status='payment_problem',
            changed_at=timezone.now() - timedelta(minutes=2)
        )
        
        history3 = ChildStatusHistory.objects.create(
            child=self.child,
            previous_status='payment_problem',
            new_status='active',
            changed_at=timezone.now() - timedelta(minutes=1)
        )
        
        history_list = list(ChildStatusHistory.objects.filter(child=self.child))
        # Most recent first
        self.assertEqual(history_list[0], history3)
        self.assertEqual(history_list[1], history2)
        self.assertEqual(history_list[2], history1)
    
    def test_status_history_cascade_delete_with_child(self):
        """Test status history is deleted when child is deleted"""
        history = ChildStatusHistory.objects.create(
            child=self.child,
            previous_status='active',
            new_status='inactive'
        )
        
        history_id = history.id
        self.child.delete()
        
        with self.assertRaises(ChildStatusHistory.DoesNotExist):
            ChildStatusHistory.objects.get(id=history_id)
    
    def test_status_history_tracks_churn(self):
        """Test status history can track churn (active -> inactive transitions)"""
        # Simulate a child joining and then leaving
        history1 = ChildStatusHistory.objects.create(
            child=self.child,
            previous_status='pending',
            new_status='active',
            reason='נרשם לחוג'
        )
        
        history2 = ChildStatusHistory.objects.create(
            child=self.child,
            previous_status='active',
            new_status='inactive',
            reason='עזב את החוג'
        )
        
        # Verify we can identify churn
        churn_events = ChildStatusHistory.objects.filter(
            child=self.child,
            previous_status='active',
            new_status='inactive'
        )
        
        self.assertEqual(churn_events.count(), 1)
        self.assertEqual(churn_events.first().reason, 'עזב את החוג')
