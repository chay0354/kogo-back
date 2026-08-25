from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.courses.models import LessonBundle
from apps.customers.models import Payment, RecurringPayment


class BundleStoSyncAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        User = get_user_model()
        self.user = User.objects.create_user(
            username='manager-sto-sync@test.com',
            email='manager-sto-sync@test.com',
            password='pass12345!',
            is_active=True,
        )
        UserProfile.objects.update_or_create(
            user=self.user, defaults={'role': UserProfile.ROLE_MANAGER}
        )
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

        self.course = TestDataFactory.create_course(price=Decimal('260.00'))
        self.lesson_a = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.lesson_b = TestDataFactory.create_lesson(course=self.course, day_of_week=3)
        self.bundle = LessonBundle.objects.create(course=self.course, combined_price=Decimal('360.00'))
        self.bundle.lessons.set([self.lesson_a, self.lesson_b])

    def _sto(self, first_name, *, amount, created_offset_minutes, extra_lesson=None, discount=Decimal('10.00')):
        family = TestDataFactory.create_family(phone=f'050{created_offset_minutes:07d}')
        child = TestDataFactory.create_child(family=family, first_name=first_name, last_name='בדיקה')
        payment = Payment.objects.create(
            child=child,
            family=family,
            branch=self.course.branch,
            lesson=self.lesson_a,
            bundle=self.bundle,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=amount + discount,
            discount_amount=discount,
            final_amount=amount,
        )
        rp = RecurringPayment.objects.create(
            child=child,
            initial_payment=payment,
            status='active',
            amount=amount,
            base_amount=amount + discount,
            discount_amount=discount,
            discount_details=[{
                'name': 'הרשמה מוקדמת',
                'type': 'fixed',
                'value': str(discount),
                'amount_deducted': str(discount),
            }],
            billing_day=1,
            start_date=date(2026, 8, 17),
            next_billing_date=date(2026, 9, 1),
            tranzila_token='shared-token',
            card_expire_month=12,
            card_expire_year=2028,
        )
        RecurringPayment.objects.filter(pk=rp.pk).update(
            created_at=timezone.now() - timedelta(days=10) + timedelta(minutes=created_offset_minutes),
        )
        rp.refresh_from_db()
        if extra_lesson is not None:
            extra_payment = Payment.objects.create(
                child=child,
                family=family,
                branch=self.course.branch,
                lesson=extra_lesson,
                bundle=self.bundle,
                payment_type='recurring_subscription',
                status='completed',
                base_amount=amount + discount,
                discount_amount=discount,
                final_amount=amount,
            )
            extra = RecurringPayment.objects.create(
                child=child,
                initial_payment=extra_payment,
                status='active',
                amount=amount,
                base_amount=amount + discount,
                discount_amount=discount,
                billing_day=1,
                start_date=date(2026, 8, 17),
                next_billing_date=date(2026, 9, 1),
                tranzila_token='shared-token',
            )
            RecurringPayment.objects.filter(pk=extra.pk).update(
                created_at=rp.created_at + timedelta(minutes=1),
            )
        return child, rp

    def _once_a_week(self, first_name):
        family = TestDataFactory.create_family(phone='0509999999')
        child = TestDataFactory.create_child(family=family, first_name=first_name, last_name='יחיד')
        payment = Payment.objects.create(
            child=child,
            family=family,
            branch=self.course.branch,
            lesson=self.lesson_a,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('260.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('260.00'),
        )
        return RecurringPayment.objects.create(
            child=child,
            initial_payment=payment,
            status='active',
            amount=Decimal('260.00'),
            billing_day=1,
            start_date=date(2026, 8, 1),
            next_billing_date=date(2026, 9, 1),
        )

    @patch('apps.customers.bundle_sto_sync.TranzilaService.production')
    def test_syncs_earliest_five_and_cancels_duplicate_locally(self, mock_production):
        mock_production.return_value.sync_standing_order_to_amount.return_value = {
            'success': True,
            'sto_id': 4411,
            'action': 'updated',
            'inactivated': [4412],
        }
        names = ['אלון', 'בניה', 'גיל', 'דור', 'הדר', 'ויויאן']
        children = []
        for index, name in enumerate(names):
            extra = self.lesson_b if name == 'אלון' else None
            child, _ = self._sto(name, amount=Decimal('170.00'), created_offset_minutes=index, extra_lesson=extra)
            children.append(child)
        once_a_week = self._once_a_week('יחיד')

        res = self.client.post(
            '/api/v1/customers/recurring-payments/sync-bundle-amounts/',
            {'limit': 5},
            format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body['synced_count'], 5)
        self.assertEqual(body['failed_count'], 0)
        self.assertEqual(body['remaining'], 1)
        synced_names = [row['child'].split()[0] for row in body['synced']]
        self.assertEqual(synced_names, names[:5])
        self.assertEqual(body['synced'][0]['new_amount'], '350.00')
        self.assertEqual(body['synced'][0]['tranzila_sto_id'], '4411')
        self.assertEqual(len(body['synced'][0]['cancelled_ids']), 1)
        self.assertEqual(mock_production.return_value.sync_standing_order_to_amount.call_count, 5)
        first_call = mock_production.return_value.sync_standing_order_to_amount.call_args_list[0]
        self.assertEqual(first_call.kwargs['amount'], Decimal('350.00'))
        self.assertEqual(first_call.kwargs['token'], 'shared-token')

        first = RecurringPayment.objects.filter(child=children[0], status='active')
        self.assertEqual(first.count(), 1)
        kept = first.get()
        self.assertEqual(kept.amount, Decimal('170.00'))
        self.assertEqual(kept.pending_amount, Decimal('350.00'))
        self.assertEqual(kept.tranzila_recurring_index, '4411')
        self.assertEqual(
            RecurringPayment.objects.filter(child=children[0], status='cancelled').count(),
            1,
        )

        sixth = RecurringPayment.objects.get(child=children[5], status='active')
        self.assertEqual(sixth.amount, Decimal('170.00'))
        self.assertIsNone(sixth.pending_amount)
        once_a_week.refresh_from_db()
        self.assertEqual(once_a_week.amount, Decimal('260.00'))
        self.assertEqual(once_a_week.status, 'active')
        self.assertIsNone(once_a_week.pending_amount)

    @patch('apps.customers.bundle_sto_sync.TranzilaService.production')
    def test_second_click_syncs_the_remaining_child(self, mock_production):
        mock_production.return_value.sync_standing_order_to_amount.return_value = {
            'success': True,
            'sto_id': 4411,
            'action': 'updated',
            'inactivated': [],
        }
        for index, name in enumerate(['אלון', 'בניה', 'גיל', 'דור', 'הדר', 'ויויאן']):
            self._sto(name, amount=Decimal('170.00'), created_offset_minutes=index)

        first = self.client.post(
            '/api/v1/customers/recurring-payments/sync-bundle-amounts/',
            {'limit': 5},
            format='json',
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            '/api/v1/customers/recurring-payments/sync-bundle-amounts/',
            {'limit': 5},
            format='json',
        )
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json()['synced_count'], 1)
        self.assertEqual(second.json()['remaining'], 0)
        self.assertEqual(second.json()['synced'][0]['child'].split()[0], 'ויויאן')

    @patch('apps.customers.bundle_sto_sync.TranzilaService.production')
    def test_crm_billed_child_is_fixed_without_handing_billing_to_tranzila(self, mock_production):
        mock_production.return_value.sync_standing_order_to_amount.return_value = {
            'success': True,
            'sto_id': None,
            'action': 'none',
            'inactivated': [],
        }
        child, rp = self._sto('אלון', amount=Decimal('170.00'), created_offset_minutes=0, extra_lesson=self.lesson_b)

        res = self.client.post(
            '/api/v1/customers/recurring-payments/sync-bundle-amounts/',
            {'limit': 5},
            format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body['synced_count'], 1)
        self.assertEqual(body['synced'][0]['new_amount'], '350.00')
        self.assertEqual(body['synced'][0]['tranzila_sto_id'], '')

        rp.refresh_from_db()
        self.assertEqual(rp.pending_amount, Decimal('350.00'))
        # Empty index keeps the row in the CRM cron queryset, which is the biller here.
        self.assertEqual(rp.tranzila_recurring_index, '')
        self.assertEqual(
            RecurringPayment.objects.filter(child=child, status='active').count(),
            1,
        )

    @patch('apps.customers.bundle_sto_sync.TranzilaService.production')
    def test_tranzila_failure_does_not_change_crm(self, mock_production):
        mock_production.return_value.sync_standing_order_to_amount.return_value = {
            'success': False,
            'error': 'Authorization failed',
        }
        child, rp = self._sto('אלון', amount=Decimal('170.00'), created_offset_minutes=0, extra_lesson=self.lesson_b)
        res = self.client.post(
            '/api/v1/customers/recurring-payments/sync-bundle-amounts/',
            {'limit': 5},
            format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body['synced_count'], 0)
        self.assertEqual(body['failed_count'], 1)
        self.assertIn('Authorization', body['failed'][0]['error'])
        rp.refresh_from_db()
        self.assertEqual(rp.status, 'active')
        self.assertEqual(rp.amount, Decimal('170.00'))
        self.assertIsNone(rp.pending_amount)
        self.assertEqual(
            RecurringPayment.objects.filter(child=child, status='active').count(),
            2,
        )
