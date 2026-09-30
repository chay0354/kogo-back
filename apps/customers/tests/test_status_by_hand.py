"""
A status changed by hand says why and is kept; money that stops is felt at once.

The owner's decisions of 30.9.2026:
  * a manual change of a child's status requires a written reason, and is
    recorded with who made it — and the child's card shows the history;
  * the morning routine does not overrule that person, it names the child;
  * cancelling a standing order or a cheque plan, or refunding a payment,
    works the status out again right then, not the next morning.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APITestCase

from apps.core import morning_fixes
from apps.core.models import UserProfile
from apps.core.morning_fixes import fix_child_statuses
from apps.core.payment_service import PaymentService
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.child_status import MANUAL_REASON_PREFIX
from apps.customers.models import Child, Payment, RecurringPayment, TranzilaTransaction
from apps.customers.status_history_models import ChildStatusHistory
from apps.enrollments.models import LessonEnrollment

User = get_user_model()
TODAY = date.today()


def _manager(name='manager-status@test'):
    user = User.objects.create_user(username=name, password='pw-for-tests', first_name='דנה', last_name='משרד')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = UserProfile.ROLE_MANAGER
    profile.save(update_fields=['role'])
    return User.objects.get(pk=user.pk)


class StatusChangedByHandTests(APITestCase):
    def setUp(self):
        self.user = _manager()
        self.client.force_authenticate(self.user)
        self.child = TestDataFactory.create_child(status='pending')
        self.url = f'/api/v1/customers/children/{self.child.id}/'

    def test_a_status_change_without_a_reason_is_refused(self):
        res = self.client.patch(self.url, {'status': 'inactive'}, format='json')
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn('status_reason', res.json())
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'pending')

    def test_a_blank_reason_is_no_reason(self):
        res = self.client.patch(self.url, {'status': 'inactive', 'status_reason': '   '}, format='json')
        self.assertEqual(res.status_code, 400, res.content)

    def test_the_change_is_recorded_once_with_who_and_why(self):
        res = self.client.patch(
            self.url, {'status': 'inactive', 'status_reason': 'ההורה הודיע בטלפון שעוזבים'}, format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'inactive')
        row = ChildStatusHistory.objects.get(child=self.child)
        self.assertEqual((row.previous_status, row.new_status), ('pending', 'inactive'))
        self.assertEqual(row.changed_by, self.user)
        self.assertTrue(row.reason.startswith(MANUAL_REASON_PREFIX))
        self.assertIn('ההורה הודיע בטלפון שעוזבים', row.reason)

    def test_saving_the_same_status_or_other_details_needs_no_reason(self):
        res = self.client.patch(self.url, {'status': 'pending', 'notes': 'הערה'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        res = self.client.patch(self.url, {'notes': 'הערה אחרת'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(ChildStatusHistory.objects.filter(child=self.child).exists())

    def test_the_card_reads_the_history_newest_first(self):
        self.client.patch(self.url, {'status': 'inactive', 'status_reason': 'עזבו'}, format='json')
        self.client.patch(self.url, {'status': 'pending', 'status_reason': 'חזרו לשאול'}, format='json')
        res = self.client.get(f'{self.url}status-history/')
        self.assertEqual(res.status_code, 200, res.content)
        rows = res.json()
        self.assertEqual([row['new_status'] for row in rows], ['pending', 'inactive'])
        self.assertEqual(rows[0]['new_label'], 'בתהליך רישום')
        self.assertEqual(rows[0]['previous_label'], 'לא פעיל')
        self.assertIn('חזרו לשאול', rows[0]['reason'])
        self.assertEqual(rows[0]['changed_by_name'], 'דנה משרד')

    def test_an_automatic_change_shows_no_name(self):
        ChildStatusHistory.objects.create(
            child=self.child, previous_status='trial_signed', new_status='pending', reason='תוקן אוטומטית',
        )
        rows = self.client.get(f'{self.url}status-history/').json()
        self.assertIsNone(rows[0]['changed_by_name'])

    def test_the_recalculate_button_goes_by_the_rule_and_is_recorded(self):
        lesson = TestDataFactory.create_lesson()
        Payment.objects.create(
            child=self.child, family=self.child.family, lesson=lesson, status='completed',
            base_amount=Decimal('225'), final_amount=Decimal('225'),
        )
        res = self.client.post(f'{self.url}update_status/')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['status'], 'active')
        row = ChildStatusHistory.objects.get(child=self.child)
        self.assertEqual(row.changed_by, self.user)


class MorningLeavesAPersonsChoiceTests(TestCase):
    def test_a_status_set_by_hand_is_named_not_moved(self):
        # נרשם לניסיון set by phone, with no trial row: the rule reads בתהליך רישום,
        # a move the morning would otherwise make by itself.
        child = TestDataFactory.create_child(status='trial_signed')
        ChildStatusHistory.objects.create(
            child=child, previous_status='pending', new_status='trial_signed',
            reason=f'{MANUAL_REASON_PREFIX}: נקבע ניסיון בטלפון',
        )
        result = fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'trial_signed')
        [left] = result['needs_person']
        self.assertEqual(left['why'], morning_fixes.LEFT_SET_BY_HAND)

    def test_once_the_status_moved_on_the_old_choice_no_longer_holds_it(self):
        child = TestDataFactory.create_child(status='trial_signed')
        ChildStatusHistory.objects.create(
            child=child, previous_status='pending', new_status='active',
            reason=f'{MANUAL_REASON_PREFIX}: שילם במזומן',
        )
        fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'pending')


class MoneyStoppedTests(TestCase):
    def _fee_only_child(self):
        """Paid the registration fee; the standing order with the card is what makes them a student."""
        child = TestDataFactory.create_child(status='active')
        lesson = TestDataFactory.create_lesson()
        LessonEnrollment.objects.create(lesson=lesson, child=child, status='active', start_date=TODAY)
        Payment.objects.create(
            child=child, family=child.family, lesson=lesson, status='completed',
            base_amount=Decimal('120'), final_amount=Decimal('120'), registration_fee=Decimal('120'),
        )
        recurring = RecurringPayment.objects.create(
            child=child, amount=Decimal('225'), status='active', tranzila_token='tok',
            start_date=TODAY, next_billing_date=TODAY + timedelta(days=10),
        )
        return child, recurring

    def test_cancelling_the_standing_order_rechecks_the_status_at_once(self):
        child, recurring = self._fee_only_child()
        gateway = MagicMock()
        gateway.cancel_recurring_payment.return_value = {'success': True}
        with patch('apps.core.payment_service.TranzilaService.for_saved_card', return_value=gateway):
            self.assertTrue(PaymentService().cancel_subscription(str(recurring.id))['success'])
        child.refresh_from_db()
        self.assertEqual(child.status, 'pending')
        row = ChildStatusHistory.objects.get(child=child)
        self.assertIn('הוראת הקבע בוטלה', row.reason)

    def test_the_course_change_path_rechecks_once_at_the_end(self):
        child, recurring = self._fee_only_child()
        with patch('apps.customers.child_status.refresh_child_status') as refresh:
            gateway = MagicMock()
            gateway.cancel_recurring_payment.return_value = {'success': True}
            with patch('apps.core.payment_service.TranzilaService.for_saved_card', return_value=gateway):
                PaymentService().cancel_subscription(str(recurring.id), recheck_status=False)
        refresh.assert_not_called()

    def test_a_failed_recheck_never_undoes_the_cancellation(self):
        child, recurring = self._fee_only_child()
        gateway = MagicMock()
        gateway.cancel_recurring_payment.return_value = {'success': True}
        with patch('apps.core.payment_service.TranzilaService.for_saved_card', return_value=gateway), \
                patch('apps.customers.child_status.refresh_child_status', side_effect=RuntimeError('boom')):
            self.assertTrue(PaymentService().cancel_subscription(str(recurring.id))['success'])
        recurring.refresh_from_db()
        self.assertEqual(recurring.status, 'cancelled')

    @patch('apps.core.credit_note_email.send_credit_note_email')
    @patch('apps.core.tranzila_service.TranzilaService.refund_transaction', return_value={
        'success': True, 'transaction_id': 'REFUND_1', 'confirmation_code': 'R1',
        'response_code': '000', 'message': 'ok', 'raw_response': {},
    })
    def test_a_refund_rechecks_the_status_at_once(self, _refund, _send):
        child = TestDataFactory.create_child(status='active')
        payment = Payment.objects.create(
            child=child, family=child.family, lesson=TestDataFactory.create_lesson(), status='completed',
            payment_type='recurring_subscription', base_amount=Decimal('236'), final_amount=Decimal('236'),
        )
        payment.tranzila_transaction = TranzilaTransaction.objects.create(
            transaction_id='TRX_R', confirmation_code='AUTH_R', transaction_type='recurring_charge',
            is_successful=True, idempotency_key='status-refund-1',
        )
        payment.save(update_fields=['tranzila_transaction'])

        self.assertTrue(PaymentService().refund_payment(str(payment.id), reason='ביטול')['success'])
        child.refresh_from_db()
        self.assertEqual(child.status, 'pending')
        self.assertIn('התשלום זוכה', ChildStatusHistory.objects.get(child=child).reason)

    def test_cancelling_a_cheque_plan_rechecks_the_status_at_once(self):
        from rest_framework.test import APIClient

        from apps.documents.check_plans import register_check_plan

        child = TestDataFactory.create_child(status='pending')
        plan = register_check_plan(
            child_id=str(child.id), lesson_id=str(TestDataFactory.create_lesson().id),
            checks=[{'date': (TODAY + timedelta(days=40)).isoformat(), 'bank': 'דיסקונט',
                     'amount': '100', 'check_number': '4001'}],
        )
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')

        client = APIClient()
        client.force_authenticate(_manager('manager-cheques@test'))
        res = client.post(f'/api/v1/documents/check-plans/{plan.id}/cancel/')
        self.assertEqual(res.status_code, 200, res.content)
        child.refresh_from_db()
        self.assertNotEqual(child.status, 'active')
        self.assertTrue(
            ChildStatusHistory.objects.filter(child=child, reason__contains='תוכנית הצ׳קים בוטלה').exists()
        )
