"""A lesson change that changes the price: quote, prorated charge, scheduled downgrade."""
from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson, LessonBundle
from apps.customers.models import Child, Family, Parent, Payment, RecurringChargeOverride, RecurringPayment, TranzilaTransaction
from apps.enrollments.change_pricing import apply_due_scheduled_unit_changes, quote_unit_change
from apps.enrollments.models import LessonEnrollment, ScheduledUnitChange

TODAY = date(2026, 9, 8)          # a Tuesday; Mondays left: 14, 21, 28 (of 4); Thursdays left: 10, 17, 24 (of 4)
OK_CHARGE = {'success': True, 'transaction_id': 'T-diff', 'confirmation_code': 'C1', 'response_code': '000', 'raw_response': {}}


class _Base(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name='B1')
        self.room = Room.objects.create(branch=self.branch, name='Studio', capacity=20)
        ctype = CourseType.objects.create(name='Capoeira')
        self.course_once = Course.objects.create(course_type=ctype, name='קפוארה יום רביעי', price=260, capacity=10, branch=self.branch)
        self.course_twice = Course.objects.create(course_type=ctype, name='קפוארה שני+חמישי', price=260, capacity=10, branch=self.branch)
        self.wed = Lesson.objects.create(course=self.course_once, room=self.room, day_of_week=3, start_time=time(16, 45), end_time=time(17, 30))
        self.mon = Lesson.objects.create(course=self.course_twice, room=self.room, day_of_week=1, start_time=time(16, 45), end_time=time(17, 30))
        self.thu = Lesson.objects.create(course=self.course_twice, room=self.room, day_of_week=4, start_time=time(16, 45), end_time=time(17, 30))
        self.bundle = LessonBundle.objects.create(course=self.course_twice, combined_price=Decimal('335.00'))
        self.bundle.lessons.set([self.mon, self.thu])

        self.family = Family.objects.create(name='Cohen', phone='0501234567', branch=self.branch)
        Parent.objects.create(family=self.family, first_name='Avi', last_name='Cohen', phone='0501234567', is_primary=True)
        self.child = Child.objects.create(family=self.family, first_name='Noa', last_name='Cohen', birth_date=date(2018, 1, 1), gender='female', status='active')
        self.enrollment = LessonEnrollment.objects.create(lesson=self.wed, child=self.child, status='active', start_date=date(2026, 8, 1))
        self.payment = Payment.objects.create(
            child=self.child, family=self.family, branch=self.branch, lesson=self.wed,
            payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('260.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('260.00'),
        )
        self.recurring = RecurringPayment.objects.create(
            child=self.child, initial_payment=self.payment, status='active',
            tranzila_token='tok_saved', card_expire_month=12, card_expire_year=2030,
            base_amount=Decimal('260.00'), amount=Decimal('260.00'), billing_day=1,
            start_date=date(2026, 8, 1), next_billing_date=date(2026, 10, 1),
        )
        User = get_user_model()
        self.manager = User.objects.create_user(username='m@test.com', email='m@test.com', password='x')
        UserProfile.objects.update_or_create(user=self.manager, defaults={'role': UserProfile.ROLE_MANAGER})
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=self.manager).key}')
        self.url = f'/api/v1/enrollments/lesson-enrollments/{self.enrollment.id}/change-lesson/'

    def _today(self):
        return patch('apps.enrollments.change_pricing.timezone.now', return_value=__import__('django').utils.timezone.make_aware(
            __import__('datetime').datetime(2026, 9, 8, 10, 0)))


class QuoteTest(_Base):
    def test_same_price_is_same(self):
        other = Lesson.objects.create(course=self.course_once, room=self.room, day_of_week=0, start_time=time(17, 0), end_time=time(18, 0))
        q = quote_unit_change(enrollment=self.enrollment, target_lessons=[other], target_bundle=None, today=TODAY)
        self.assertEqual(q['direction'], 'same')
        self.assertEqual(q['new_amount'], '260.00')

    def test_upgrade_to_a_bundle_prorates_the_difference_over_the_remaining_occurrences(self):
        q = quote_unit_change(enrollment=self.enrollment, target_lessons=[self.mon, self.thu], target_bundle=self.bundle, today=TODAY)
        self.assertEqual(q['direction'], 'up')
        self.assertEqual(q['current_amount'], '260.00')
        self.assertEqual(q['new_amount'], '335.00')
        self.assertEqual(q['difference'], '75.00')
        self.assertEqual((q['remaining_occurrences'], q['total_occurrences']), (6, 8))
        self.assertEqual(q['prorated_difference'], '56.25')      # 75 × 6/8
        self.assertEqual(q['effective_date'], '2026-10-01')
        self.assertTrue(q['has_saved_card'])

    def test_downgrade_has_no_charge_and_a_scheduled_date(self):
        self.enrollment.lesson = self.mon
        self.enrollment.bundle = self.bundle
        self.enrollment.save()
        LessonEnrollment.objects.create(lesson=self.thu, child=self.child, status='active', bundle=self.bundle)
        self.recurring.amount = Decimal('335.00'); self.recurring.base_amount = Decimal('335.00'); self.recurring.save()
        self.payment.lesson = self.mon; self.payment.bundle = self.bundle; self.payment.save()
        q = quote_unit_change(enrollment=self.enrollment, target_lessons=[self.wed], target_bundle=None, today=TODAY)
        self.assertEqual(q['direction'], 'down')
        self.assertEqual(q['new_amount'], '260.00')
        self.assertEqual(q['prorated_difference'], '0.00')

    def test_a_tranzila_managed_order_is_blocked(self):
        self.recurring.tranzila_recurring_index = 'sto-1'; self.recurring.save()
        q = quote_unit_change(enrollment=self.enrollment, target_lessons=[self.mon, self.thu], target_bundle=self.bundle, today=TODAY)
        self.assertTrue(q['blocked'])

    def test_quote_endpoint(self):
        res = self.client.post(f'{self.url}quote/', {'bundle_id': str(self.bundle.id)}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['direction'], 'up')
        self.assertEqual(res.data['new_amount'], '335.00')


class UpgradeTest(_Base):
    def _post(self, **body):
        payload = {'bundle_id': str(self.bundle.id)}
        payload.update(body)
        with self._today():
            return self.client.post(self.url, payload, format='json')

    def test_without_the_quoted_amount_nothing_happens(self):
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod:
            res = self._post()
            prod.return_value.charge_with_token.assert_not_called()
        self.assertEqual(res.status_code, 400, res.content)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.lesson_id, self.wed.id)
        self.recurring.refresh_from_db()
        self.assertIsNone(self.recurring.pending_amount)

    def test_a_stale_quote_is_refused(self):
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod:
            res = self._post(expected_new_amount='300.00')
            prod.return_value.charge_with_token.assert_not_called()
        self.assertEqual(res.status_code, 400)

    def test_confirmed_upgrade_charges_the_difference_moves_the_child_and_schedules_the_amount(self):
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod, \
             patch('apps.enrollments.change_pricing.PaymentService._create_invoice_from_payment') as invoice:
            prod.return_value.charge_with_token.return_value = dict(OK_CHARGE)
            res = self._post(expected_new_amount='335.00')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['applied'], 'now')
        self.assertEqual(res.data['charged'], '56.25')
        kwargs = prod.return_value.charge_with_token.call_args.kwargs
        self.assertEqual(kwargs['amount'], Decimal('56.25'))
        self.assertEqual(kwargs['token'], 'tok_saved')
        self.assertEqual(kwargs['expire_year'], 2030)
        diff = Payment.objects.get(payment_type='one_time', child=self.child)
        self.assertEqual(diff.status, 'completed')
        self.assertEqual(diff.final_amount, Decimal('56.25'))
        self.assertEqual(diff.lesson_id, self.mon.id)
        self.assertTrue(TranzilaTransaction.objects.filter(idempotency_key=f'change_diff_{diff.id}').exists())
        invoice.assert_called_once()
        active = list(LessonEnrollment.objects.filter(child=self.child, status='active').order_by('lesson__day_of_week'))
        self.assertEqual([row.lesson_id for row in active], [self.mon.id, self.thu.id])
        self.assertTrue(all(row.bundle_id == self.bundle.id for row in active))
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.amount, Decimal('260.00'))              # this month is paid
        self.assertEqual(self.recurring.pending_amount, Decimal('335.00'))
        self.assertEqual(self.recurring.pending_amount_effective_date, date(2026, 10, 1))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.final_amount, Decimal('260.00'))           # history untouched
        self.assertEqual(self.payment.bundle_id, self.bundle.id)

    def test_a_declined_difference_leaves_everything_as_it_was(self):
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod:
            prod.return_value.charge_with_token.return_value = {'success': False, 'error': 'declined'}
            res = self._post(expected_new_amount='335.00')
        self.assertEqual(res.status_code, 400)
        self.assertIn('נדחה', res.data['error'])
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.lesson_id, self.wed.id)
        self.recurring.refresh_from_db()
        self.assertIsNone(self.recurring.pending_amount)
        self.assertEqual(Payment.objects.get(payment_type='one_time').status, 'failed')

    def test_an_uncertain_answer_freezes_and_a_second_try_is_refused(self):
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod:
            prod.return_value.charge_with_token.return_value = {'success': False, 'uncertain': True, 'error': 'timeout'}
            res = self._post(expected_new_amount='335.00')
            self.assertEqual(res.status_code, 409)
            self.assertEqual(Payment.objects.get(payment_type='one_time').status, 'processing')
            prod.return_value.charge_with_token.return_value = dict(OK_CHARGE)
            res = self._post(expected_new_amount='335.00')
            self.assertEqual(res.status_code, 409)
            self.assertEqual(prod.return_value.charge_with_token.call_count, 1)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.lesson_id, self.wed.id)

    def test_no_saved_card_folds_the_difference_into_next_month(self):
        self.recurring.tranzila_token = ''; self.recurring.save()
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod:
            res = self._post(expected_new_amount='335.00')
            prod.return_value.charge_with_token.assert_not_called()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data['folded_into_next_month'])
        override = RecurringChargeOverride.objects.get(recurring_payment=self.recurring)
        self.assertEqual(override.billing_month, date(2026, 10, 1))
        self.assertEqual(override.amount, Decimal('391.25'))                    # 335 + 56.25, once
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.pending_amount, Decimal('335.00'))
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.lesson_id, self.mon.id)

    def test_no_standing_order_moves_without_pricing(self):
        self.recurring.delete()
        res = self._post()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['applied'], 'now')
        self.assertIsNone(res.data['charged'])


class DowngradeTest(_Base):
    def setUp(self):
        super().setUp()
        self.enrollment.lesson = self.mon
        self.enrollment.bundle = self.bundle
        self.enrollment.save()
        self.thu_row = LessonEnrollment.objects.create(lesson=self.thu, child=self.child, status='active', bundle=self.bundle)
        self.recurring.amount = Decimal('335.00'); self.recurring.base_amount = Decimal('335.00'); self.recurring.save()
        self.payment.lesson = self.mon; self.payment.bundle = self.bundle; self.payment.save()

    def _post(self, **body):
        payload = {'lesson_id': str(self.wed.id)}
        payload.update(body)
        with self._today():
            return self.client.post(self.url, payload, format='json')

    def test_downgrade_is_scheduled_and_nothing_moves_this_month(self):
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod:
            res = self._post(expected_new_amount='260.00')
            prod.return_value.charge_with_token.assert_not_called()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['applied'], 'scheduled')
        self.assertEqual(res.data['scheduled_change']['effective_date'], '2026-10-01')
        self.assertEqual(res.data['scheduled_change']['new_amount'], '260.00')
        self.assertEqual({row['lesson_id'] for row in res.data['enrollments']}, {str(self.mon.id), str(self.thu.id)})
        self.assertEqual(res.data['enrollments'][0]['scheduled_change']['target_label'], res.data['scheduled_change']['target_label'])
        active = set(LessonEnrollment.objects.filter(child=self.child, status='active').values_list('lesson_id', flat=True))
        self.assertEqual(active, {self.mon.id, self.thu.id})
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.amount, Decimal('335.00'))
        self.assertEqual(self.recurring.pending_amount, Decimal('260.00'))
        self.assertEqual(self.recurring.pending_amount_effective_date, date(2026, 10, 1))
        change = ScheduledUnitChange.objects.get(child=self.child)
        self.assertTrue(change.is_pending)
        self.assertEqual(set(change.target_lessons.values_list('id', flat=True)), {self.wed.id})

    def test_the_customers_list_shows_the_scheduled_change(self):
        self._post(expected_new_amount='260.00')
        res = self.client.get('/api/v1/customers/children/', {'family': str(self.family.id)})
        rows = res.data['results'][0]['enrollments']
        self.assertTrue(all(row['scheduled_change'] and row['scheduled_change']['new_amount'] == '260.00' for row in rows))

    def test_a_second_change_waits_for_the_scheduled_one(self):
        self._post(expected_new_amount='260.00')
        res = self._post(expected_new_amount='260.00')
        self.assertEqual(res.status_code, 400)
        self.assertIn('מתוזמנת', res.data['error'])

    def test_cancelling_restores_the_amount(self):
        self._post(expected_new_amount='260.00')
        res = self.client.post(f'/api/v1/enrollments/lesson-enrollments/{self.enrollment.id}/cancel-scheduled-change/')
        self.assertEqual(res.status_code, 200, res.content)
        self.recurring.refresh_from_db()
        self.assertIsNone(self.recurring.pending_amount)
        self.assertFalse(ScheduledUnitChange.objects.get(child=self.child).is_pending)

    def test_the_cron_applies_the_change_on_its_date(self):
        self._post(expected_new_amount='260.00')
        self.assertEqual(apply_due_scheduled_unit_changes(today=date(2026, 9, 30))['applied'], 0)
        summary = apply_due_scheduled_unit_changes(today=date(2026, 10, 1))
        self.assertEqual(summary['applied'], 1)
        active = list(LessonEnrollment.objects.filter(child=self.child, status='active'))
        self.assertEqual([row.lesson_id for row in active], [self.wed.id])
        self.assertIsNone(active[0].bundle_id)
        change = ScheduledUnitChange.objects.get(child=self.child)
        self.assertIsNotNone(change.applied_at)
        # The billing cron runs the same promotion for the amount.
        from apps.customers.recurring_amount import apply_due_pending_recurring_amounts
        with patch('apps.customers.recurring_amount.timezone.localdate', return_value=date(2026, 10, 1)):
            apply_due_pending_recurring_amounts()
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.amount, Decimal('260.00'))

    def test_a_lesson_that_got_cancelled_leaves_the_change_for_a_person(self):
        self._post(expected_new_amount='260.00')
        self.wed.status = 'cancelled'; self.wed.save()
        summary = apply_due_scheduled_unit_changes(today=date(2026, 10, 1))
        self.assertEqual(summary['failed'], 1)
        change = ScheduledUnitChange.objects.get(child=self.child)
        self.assertTrue(change.last_error)
        self.assertEqual(set(LessonEnrollment.objects.filter(child=self.child, status='active').values_list('lesson_id', flat=True)), {self.mon.id, self.thu.id})


class PriceDriftReportTest(_Base):
    def test_a_child_moved_onto_a_dearer_bundle_at_the_old_price_is_listed(self):
        # The pre-2026-09-08 change flow: pointers moved, amount stayed 260 on a 335 bundle.
        self.enrollment.lesson = self.mon; self.enrollment.bundle = self.bundle; self.enrollment.save()
        LessonEnrollment.objects.create(lesson=self.thu, child=self.child, status='active', bundle=self.bundle)
        self.payment.lesson = self.mon; self.payment.bundle = self.bundle; self.payment.save()
        res = self.client.get('/api/v1/customers/recurring-payments/price-drift/')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['count'], 1)
        row = res.data['results'][0]
        self.assertEqual(row['child_name'], 'Noa Cohen')
        self.assertEqual(row['current_amount'], '260.00')
        self.assertEqual(row['expected_amount'], '335.00')
        self.assertEqual(row['direction'], 'up')

    def test_a_correctly_priced_order_is_not_listed(self):
        res = self.client.get('/api/v1/customers/recurring-payments/price-drift/')
        self.assertEqual(res.data['count'], 0)

    def test_partner_cannot_read_it(self):
        User = get_user_model()
        partner = User.objects.create_user(username='p@test.com', email='p@test.com', password='x')
        UserProfile.objects.update_or_create(user=partner, defaults={'role': UserProfile.ROLE_PARTNER})
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=partner).key}')
        self.assertEqual(client.get('/api/v1/customers/recurring-payments/price-drift/').status_code, 403)
