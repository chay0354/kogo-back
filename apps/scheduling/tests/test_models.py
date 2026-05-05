"""
Unit tests for Scheduling app models.

Tests coverage:
- ScheduleEvent: event management, is_event property
- SubscriptionLog: subscription action logging
- SubscriptionReminder: reminder tracking
- LessonCancellation: date-specific lesson cancellations
"""
from datetime import date, time, timedelta
from django.test import TestCase
from django.contrib.auth import get_user_model

from apps.core.tests.test_fixtures import TestDataFactory
from apps.scheduling.models import ScheduleEvent, SubscriptionLog, SubscriptionReminder, LessonCancellation

User = get_user_model()


class ScheduleEventModelTest(TestCase):
    """Test ScheduleEvent model"""
    
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
        self.room = TestDataFactory.create_room(branch=self.branch)
        self.instructor = TestDataFactory.create_instructor(branch=self.branch)
    
    def test_create_schedule_event(self):
        """Test creating a schedule event"""
        event = ScheduleEvent.objects.create(
            name="אירוע מיוחד",
            event_date=date.today(),
            start_time=time(10, 0),
            end_time=time(12, 0),
            event_type='one_time',
            branch=self.branch
        )
        
        self.assertEqual(event.name, "אירוע מיוחד")
        self.assertEqual(event.event_type, 'one_time')
        self.assertTrue(event.is_active)
    
    def test_schedule_event_type_choices(self):
        """Test schedule event type choices"""
        event1 = ScheduleEvent.objects.create(
            name="אירוע חד פעמי",
            event_date=date.today(),
            event_type='one_time'
        )
        
        event2 = ScheduleEvent.objects.create(
            name="אירוע שבועי",
            event_date=date.today(),
            event_type='weekly'
        )
        
        self.assertEqual(event1.event_type, 'one_time')
        self.assertEqual(event2.event_type, 'weekly')
    
    def test_schedule_event_is_event_property(self):
        """Test schedule event is_event property"""
        event = ScheduleEvent.objects.create(
            name="בדיקה",
            event_date=date.today()
        )
        
        self.assertTrue(event.is_event)
    
    def test_schedule_event_with_room(self):
        """Test schedule event with studio/room"""
        event = ScheduleEvent.objects.create(
            name="אירוע בסטודיו",
            event_date=date.today(),
            branch=self.branch,
            studio=self.room
        )
        
        self.assertEqual(event.studio, self.room)
    
    def test_schedule_event_with_assigned_instructors(self):
        """Test schedule event with assigned instructors (M2M)"""
        instructor2 = TestDataFactory.create_instructor(
            first_name="שרה",
            last_name="לוי",
            branch=self.branch
        )
        
        event = ScheduleEvent.objects.create(
            name="אירוע עם מדריכים",
            event_date=date.today(),
            branch=self.branch
        )
        
        event.assigned_instructors.add(self.instructor, instructor2)
        
        self.assertEqual(event.assigned_instructors.count(), 2)
        self.assertIn(self.instructor, event.assigned_instructors.all())
        self.assertIn(instructor2, event.assigned_instructors.all())
    
    def test_schedule_event_with_color(self):
        """Test schedule event with custom color"""
        event = ScheduleEvent.objects.create(
            name="אירוע צבעוני",
            event_date=date.today(),
            color='#FF0000'  # Red
        )
        
        self.assertEqual(event.color, '#FF0000')
    
    def test_schedule_event_default_color(self):
        """Test schedule event has default purple color"""
        event = ScheduleEvent.objects.create(
            name="אירוע ברירת מחדל",
            event_date=date.today()
        )
        
        self.assertEqual(event.color, '#9333ea')
    
    def test_schedule_event_with_files(self):
        """Test schedule event with attached files (JSON)"""
        files = [
            'https://example.com/file1.pdf',
            'https://example.com/file2.jpg'
        ]
        
        event = ScheduleEvent.objects.create(
            name="אירוע עם קבצים",
            event_date=date.today(),
            files=files
        )
        
        self.assertEqual(len(event.files), 2)
        self.assertEqual(event.files[0], 'https://example.com/file1.pdf')
    
    def test_schedule_event_str_representation(self):
        """Test schedule event string representation"""
        event_date = date(2024, 3, 15)
        event = ScheduleEvent.objects.create(
            name="אירוע בדיקה",
            event_date=event_date
        )
        
        str_repr = str(event)
        self.assertIn("אירוע בדיקה", str_repr)
        self.assertIn('15/03/2024', str_repr)
    
    def test_schedule_event_ordering(self):
        """Test schedule events are ordered by event_date, start_time"""
        event1 = ScheduleEvent.objects.create(
            name="אירוע 1",
            event_date=date.today(),
            start_time=time(15, 0)
        )
        
        event2 = ScheduleEvent.objects.create(
            name="אירוע 2",
            event_date=date.today(),
            start_time=time(10, 0)
        )
        
        event3 = ScheduleEvent.objects.create(
            name="אירוע 3",
            event_date=date.today() + timedelta(days=1),
            start_time=time(10, 0)
        )
        
        events = list(ScheduleEvent.objects.all())
        self.assertEqual(events[0], event2)  # Today 10:00
        self.assertEqual(events[1], event1)  # Today 15:00
        self.assertEqual(events[2], event3)  # Tomorrow 10:00


class SubscriptionLogModelTest(TestCase):
    """Test SubscriptionLog model"""
    
    def setUp(self):
        self.child = TestDataFactory.create_child()
    
    def test_create_subscription_log(self):
        """Test creating a subscription log"""
        log = SubscriptionLog.objects.create(
            child=self.child,
            action_type='renew',
            previous_status='active',
            new_status='active',
            previous_end_date=date.today(),
            new_end_date=date.today() + timedelta(days=30),
            performed_by='admin'
        )
        
        self.assertEqual(log.action_type, 'renew')
        self.assertEqual(log.performed_by, 'admin')
    
    def test_subscription_log_action_types(self):
        """Test subscription log action type choices"""
        actions = ['renew', 'cancel', 'expire']
        
        for action in actions:
            log = SubscriptionLog.objects.create(
                child=self.child,
                action_type=action
            )
            self.assertEqual(log.action_type, action)
    
    def test_subscription_log_with_reason(self):
        """Test subscription log with reason"""
        log = SubscriptionLog.objects.create(
            child=self.child,
            action_type='cancel',
            previous_status='active',
            new_status='cancelled',
            reason="לבקשת ההורה"
        )
        
        self.assertEqual(log.reason, "לבקשת ההורה")
    
    def test_subscription_log_str_representation(self):
        """Test subscription log string representation"""
        log = SubscriptionLog.objects.create(
            child=self.child,
            action_type='renew'
        )
        
        str_repr = str(log)
        self.assertIn(self.child.full_name, str_repr)
    
    def test_subscription_log_cascade_delete_with_child(self):
        """Test subscription log is deleted when child is deleted"""
        log = SubscriptionLog.objects.create(
            child=self.child,
            action_type='renew'
        )
        
        log_id = log.id
        self.child.delete()
        
        with self.assertRaises(SubscriptionLog.DoesNotExist):
            SubscriptionLog.objects.get(id=log_id)


class SubscriptionReminderModelTest(TestCase):
    """Test SubscriptionReminder model"""
    
    def setUp(self):
        self.child = TestDataFactory.create_child()
    
    def test_create_subscription_reminder(self):
        """Test creating a subscription reminder"""
        reminder = SubscriptionReminder.objects.create(
            child=self.child,
            reminder_type='renewal',
            days_before_end=7,
            phone_number='050-1234567',
            message_content='המנוי שלך עומד לפוג בעוד 7 ימים',
            status='pending'
        )
        
        self.assertEqual(reminder.days_before_end, 7)
        self.assertEqual(reminder.status, 'pending')
    
    def test_subscription_reminder_status_choices(self):
        """Test subscription reminder status choices"""
        statuses = ['pending', 'sent', 'failed']
        
        for idx, status in enumerate(statuses):
            reminder = SubscriptionReminder.objects.create(
                child=self.child,
                reminder_type=f'type_{idx}',
                days_before_end=7,
                phone_number='050-1234567',
                message_content='תזכורת',
                status=status
            )
            self.assertEqual(reminder.status, status)
    
    def test_subscription_reminder_str_representation(self):
        """Test subscription reminder string representation"""
        reminder = SubscriptionReminder.objects.create(
            child=self.child,
            reminder_type='renewal',
            days_before_end=7,
            phone_number='050-1234567',
            message_content='תזכורת'
        )
        
        str_repr = str(reminder)
        self.assertIn(self.child.full_name, str_repr)
        self.assertIn('renewal', str_repr)
    
    def test_subscription_reminder_cascade_delete_with_child(self):
        """Test subscription reminder is deleted when child is deleted"""
        reminder = SubscriptionReminder.objects.create(
            child=self.child,
            reminder_type='renewal',
            days_before_end=7,
            phone_number='050-1234567',
            message_content='תזכורת'
        )
        
        reminder_id = reminder.id
        self.child.delete()
        
        with self.assertRaises(SubscriptionReminder.DoesNotExist):
            SubscriptionReminder.objects.get(id=reminder_id)


class LessonCancellationModelTest(TestCase):
    """Test LessonCancellation model"""
    
    def setUp(self):
        self.lesson = TestDataFactory.create_lesson()
        self.user = User.objects.create_user(
            username='admin@test.com',
            password='testpass123'
        )
    
    def test_create_lesson_cancellation(self):
        """Test creating a lesson cancellation"""
        occurrence_date = date.today() + timedelta(days=7)
        
        cancellation = LessonCancellation.objects.create(
            lesson=self.lesson,
            occurrence_date=occurrence_date,
            reason="מדריך חולה",
            created_by=self.user
        )
        
        self.assertEqual(cancellation.lesson, self.lesson)
        self.assertEqual(cancellation.occurrence_date, occurrence_date)
        self.assertEqual(cancellation.reason, "מדריך חולה")
        self.assertEqual(cancellation.created_by, self.user)
    
    def test_lesson_cancellation_unique_constraint(self):
        """Test lesson cancellation has unique constraint on lesson+occurrence_date"""
        occurrence_date = date.today() + timedelta(days=7)
        
        LessonCancellation.objects.create(
            lesson=self.lesson,
            occurrence_date=occurrence_date
        )
        
        # Creating duplicate should raise error
        with self.assertRaises(Exception):  # IntegrityError
            LessonCancellation.objects.create(
                lesson=self.lesson,
                occurrence_date=occurrence_date
            )
    
    def test_lesson_cancellation_str_representation(self):
        """Test lesson cancellation string representation"""
        occurrence_date = date.today() + timedelta(days=7)
        
        cancellation = LessonCancellation.objects.create(
            lesson=self.lesson,
            occurrence_date=occurrence_date
        )
        
        str_repr = str(cancellation)
        self.assertIn(str(occurrence_date), str_repr)
    
    def test_lesson_multiple_cancellations(self):
        """Test lesson can have multiple cancellations for different dates"""
        date1 = date.today() + timedelta(days=7)
        date2 = date.today() + timedelta(days=14)
        date3 = date.today() + timedelta(days=21)
        
        cancellation1 = LessonCancellation.objects.create(
            lesson=self.lesson,
            occurrence_date=date1,
            reason="חג"
        )
        
        cancellation2 = LessonCancellation.objects.create(
            lesson=self.lesson,
            occurrence_date=date2,
            reason="אירוע מיוחד"
        )
        
        cancellation3 = LessonCancellation.objects.create(
            lesson=self.lesson,
            occurrence_date=date3,
            reason="מדריך בחופשה"
        )
        
        cancellations = self.lesson.cancellations.all()
        self.assertEqual(cancellations.count(), 3)
    
    def test_lesson_cancellation_cascade_delete_with_lesson(self):
        """Test lesson cancellation is deleted when lesson is deleted"""
        cancellation = LessonCancellation.objects.create(
            lesson=self.lesson,
            occurrence_date=date.today() + timedelta(days=7)
        )
        
        cancellation_id = cancellation.id
        self.lesson.delete()
        
        with self.assertRaises(LessonCancellation.DoesNotExist):
            LessonCancellation.objects.get(id=cancellation_id)
    
    def test_lesson_cancellation_ordering(self):
        """Test lesson cancellations are ordered by -occurrence_date"""
        date1 = date.today() + timedelta(days=7)
        date2 = date.today() + timedelta(days=14)
        date3 = date.today() + timedelta(days=21)
        
        cancellation1 = LessonCancellation.objects.create(
            lesson=self.lesson,
            occurrence_date=date1
        )
        
        cancellation2 = LessonCancellation.objects.create(
            lesson=self.lesson,
            occurrence_date=date2
        )
        
        cancellation3 = LessonCancellation.objects.create(
            lesson=self.lesson,
            occurrence_date=date3
        )
        
        cancellations = list(LessonCancellation.objects.filter(lesson=self.lesson))
        # Most recent date first
        self.assertEqual(cancellations[0], cancellation3)
        self.assertEqual(cancellations[1], cancellation2)
        self.assertEqual(cancellations[2], cancellation1)
