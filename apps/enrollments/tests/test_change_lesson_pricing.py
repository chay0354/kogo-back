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

    def test_no_saved_card_moves_and_leaves_the_difference_to_the_office(self):
        # The cron cannot bill an order without a card either, so no override is filed.
        self.recurring.tranzila_token = ''; self.recurring.save()
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod:
            res = self._post(expected_new_amount='335.00')
            prod.return_value.charge_with_token.assert_not_called()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['manual_collection'], '56.25')
        self.assertFalse(res.data['folded_into_next_month'])
        self.assertFalse(RecurringChargeOverride.objects.filter(recurring_payment=self.recurring).exists())
        self.assertFalse(Payment.objects.filter(payment_type='one_time').exists())
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.pending_amount, Decimal('335.00'))
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.lesson_id, self.mon.id)

    def test_a_move_that_fails_after_the_charge_freezes_the_payment_and_nothing_moves(self):
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod, \
             patch('apps.enrollments.change_pricing.replace_unit', side_effect=RuntimeError('db down')):
            prod.return_value.charge_with_token.return_value = dict(OK_CHARGE)
            res = self._post(expected_new_amount='335.00')
        self.assertEqual(res.status_code, 409, res.content)
        self.assertIn('חויב אך ההזזה נכשלה', res.data['error'])
        diff = Payment.objects.get(payment_type='one_time')
        self.assertEqual(diff.status, 'processing')                             # the guard, not 'completed'
        self.assertFalse(TranzilaTransaction.objects.filter(idempotency_key=f'change_diff_{diff.id}').exists())
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.lesson_id, self.wed.id)
        self.recurring.refresh_from_db()
        self.assertIsNone(self.recurring.pending_amount)
        # A second attempt is refused before the gateway is called again.
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod:
            res = self._post(expected_new_amount='335.00')
            prod.return_value.charge_with_token.assert_not_called()
        self.assertEqual(res.status_code, 409)

    def _frozen_difference(self):
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod:
            prod.return_value.charge_with_token.return_value = {'success': False, 'uncertain': True, 'error': 'timeout'}
            self._post(expected_new_amount='335.00')
        return Payment.objects.get(payment_type='one_time', status='processing')

    def test_resolving_a_frozen_difference_as_not_charged_frees_the_child(self):
        diff = self._frozen_difference()
        res = self.client.post(f'/api/v1/customers/payments/{diff.id}/resolve-change-difference/', {'decision': 'failed'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        diff.refresh_from_db()
        self.assertEqual(diff.status, 'failed')
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod, \
             patch('apps.enrollments.change_pricing.PaymentService._create_invoice_from_payment'):
            prod.return_value.charge_with_token.return_value = dict(OK_CHARGE)
            res = self._post(expected_new_amount='335.00')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['charged'], '56.25')

    def test_resolving_a_frozen_difference_as_charged_applies_the_move_without_a_new_charge(self):
        diff = self._frozen_difference()
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod, \
             patch('apps.enrollments.change_pricing.PaymentService._create_invoice_from_payment') as invoice, self._today():
            res = self.client.post(
                f'/api/v1/customers/payments/{diff.id}/resolve-change-difference/',
                {'decision': 'charged', 'transaction_id': 'T-manual', 'confirmation_code': 'C9'}, format='json',
            )
            prod.return_value.charge_with_token.assert_not_called()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data['moved'])
        diff.refresh_from_db()
        self.assertEqual(diff.status, 'completed')
        self.assertEqual(TranzilaTransaction.objects.get(idempotency_key=f'change_diff_{diff.id}').transaction_id, 'T-manual')
        invoice.assert_called_once()
        active = sorted(LessonEnrollment.objects.filter(child=self.child, status='active').values_list('lesson_id', flat=True), key=str)
        self.assertEqual(active, sorted([self.mon.id, self.thu.id], key=str))
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.pending_amount, Decimal('335.00'))
        # Partners and a second resolve are refused.
        self.assertEqual(self.client.post(f'/api/v1/customers/payments/{diff.id}/resolve-change-difference/', {'decision': 'failed'}, format='json').status_code, 400)

    def test_a_non_finite_amount_is_refused(self):
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod:
            res = self._post(expected_new_amount='NaN')
            prod.return_value.charge_with_token.assert_not_called()
        self.assertEqual(res.status_code, 400)

    def test_moving_to_the_same_unit_changes_nothing_and_charges_nothing(self):
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod, self._today():
            res = self.client.post(self.url, {'lesson_id': str(self.wed.id)}, format='json')
            prod.return_value.charge_with_token.assert_not_called()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data.get('unchanged'))
        self.assertFalse(Payment.objects.filter(payment_type='one_time').exists())

    def test_a_same_price_move_replaces_a_stale_pending_amount_only_after_confirmation(self):
        # An upgrade reverted the same day: 260 → 335 (pending) → back to a 260 lesson.
        self.recurring.pending_amount = Decimal('335.00'); self.recurring.pending_amount_effective_date = date(2026, 10, 1); self.recurring.save()
        same = Lesson.objects.create(course=self.course_once, room=self.room, day_of_week=0, start_time=time(17, 0), end_time=time(18, 0))
        with self._today():
            quote = self.client.post(f'{self.url}quote/', {'lesson_id': str(same.id)}, format='json').data
        self.assertEqual(quote['direction'], 'same')
        self.assertTrue(quote['clears_pending'])
        self.assertEqual(quote['pending_amount'], '335.00')
        with self._today():
            res = self.client.post(self.url, {'lesson_id': str(same.id)}, format='json')
        self.assertEqual(res.status_code, 400)
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.pending_amount, Decimal('335.00'))
        with self._today():
            res = self.client.post(self.url, {'lesson_id': str(same.id), 'expected_new_amount': '260.00'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data['cleared_pending'])
        self.recurring.refresh_from_db()
        self.assertIsNone(self.recurring.pending_amount)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.lesson_id, same.id)

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

    def test_a_lesson_that_got_cancelled_leaves_the_change_for_a_person_and_keeps_the_old_amount(self):
        self._post(expected_new_amount='260.00')
        self.wed.status = 'cancelled'; self.wed.save()
        summary = apply_due_scheduled_unit_changes(today=date(2026, 10, 1))
        self.assertEqual(summary['failed'], 1)
        self.assertEqual(len(summary['errors']), 1)
        change = ScheduledUnitChange.objects.get(child=self.child)
        self.assertTrue(change.last_error)
        self.assertTrue(change.is_pending)                                       # still there for a person
        self.assertEqual(set(LessonEnrollment.objects.filter(child=self.child, status='active').values_list('lesson_id', flat=True)), {self.mon.id, self.thu.id})
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.amount, Decimal('335.00'))
        self.assertIsNone(self.recurring.pending_amount)                         # the lower figure is not promoted

    def test_the_billing_cron_moves_the_child_before_it_promotes_and_charges(self):
        from apps.customers import recurring_billing
        self._post(expected_new_amount='260.00')
        order = []
        real_apply = recurring_billing.apply_due_pending_recurring_amounts

        def promote():
            order.append('promote')
            return real_apply()

        oct_first = __import__('django').utils.timezone.make_aware(__import__('datetime').datetime(2026, 10, 1, 7, 0))
        with patch('apps.customers.recurring_billing.timezone') as tz, \
             patch('apps.enrollments.change_pricing.timezone.now', return_value=oct_first), \
             patch('apps.customers.recurring_billing.apply_due_pending_recurring_amounts', side_effect=promote), \
             patch('apps.enrollments.change_pricing.replace_unit', side_effect=lambda **kw: order.append('move')), \
             patch('apps.customers.recurring_billing.TranzilaService') as svc:
            tz.now.return_value = oct_first
            summary = recurring_billing.process_due_recurring_charges(dry_run=True)
        self.assertEqual(summary['scheduled_changes'].get('skipped'), 'dry_run')
        self.assertEqual(order, ['promote'])                                     # a dry run moves nobody
        order.clear()
        with patch('apps.customers.recurring_billing.timezone') as tz, \
             patch('apps.enrollments.change_pricing.timezone.now', return_value=oct_first), \
             patch('apps.customers.recurring_billing.apply_due_pending_recurring_amounts', side_effect=promote), \
             patch('apps.enrollments.change_pricing.replace_unit', side_effect=lambda **kw: order.append('move')), \
             patch('apps.customers.recurring_billing.TranzilaService') as svc:
            tz.now.return_value = oct_first
            svc.production.return_value.charge_with_token.return_value = dict(OK_CHARGE)
            summary = recurring_billing.process_due_recurring_charges(dry_run=False)
        self.assertEqual(order, ['move', 'promote'])
        self.assertEqual(summary['scheduled_changes']['applied'], 1)

    def test_cancelling_after_the_amount_was_promoted_early_restores_it(self):
        self._post(expected_new_amount='260.00')
        # The recurring list promotes on read; simulate that happening before the cron moved the child.
        self.recurring.amount = Decimal('260.00'); self.recurring.base_amount = Decimal('260.00')
        self.recurring.pending_amount = None; self.recurring.pending_amount_effective_date = None; self.recurring.save()
        res = self.client.post(f'/api/v1/enrollments/lesson-enrollments/{self.enrollment.id}/cancel-scheduled-change/')
        self.assertEqual(res.status_code, 200, res.content)
        self.recurring.refresh_from_db()
        self.assertEqual(self.recurring.amount, Decimal('335.00'))
        self.assertIsNone(self.recurring.pending_amount)

    def test_a_trial_row_of_the_same_child_does_not_carry_the_tag(self):
        self._post(expected_new_amount='260.00')
        LessonEnrollment.objects.create(lesson=self.wed, child=self.child, status='active', trial_lesson_date=date(2026, 9, 16))
        res = self.client.get('/api/v1/customers/children/', {'family': str(self.family.id)})
        rows = res.data['results'][0]['enrollments']
        trial = [row for row in rows if row.get('trial_lesson_date')]
        self.assertEqual(len(trial), 1)
        self.assertIsNone(trial[0]['scheduled_change'])
        self.assertTrue(all(row['scheduled_change'] for row in rows if not row.get('trial_lesson_date')))


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


class PartnerAndRoomTest(_Base):
    def _partner_client(self):
        User = get_user_model()
        partner = User.objects.create_user(username='p@test.com', email='p@test.com', password='x')
        UserProfile.objects.update_or_create(user=partner, defaults={'role': UserProfile.ROLE_PARTNER})
        partner.profile.assigned_branches.add(self.branch)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=partner).key}')
        return client

    def test_a_partner_cannot_make_a_priced_change_but_can_move_at_the_same_price(self):
        client = self._partner_client()
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod:
            res = client.post(self.url, {'bundle_id': str(self.bundle.id), 'expected_new_amount': '335.00'}, format='json')
            prod.return_value.charge_with_token.assert_not_called()
        self.assertEqual(res.status_code, 400)
        self.assertIn('מנהל בלבד', res.data['error'])
        same = Lesson.objects.create(course=self.course_once, room=self.room, day_of_week=0, start_time=time(17, 0), end_time=time(18, 0))
        res = client.post(self.url, {'lesson_id': str(same.id)}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(client.post(f'/api/v1/enrollments/lesson-enrollments/{self.enrollment.id}/cancel-scheduled-change/').status_code, 403)

    def test_a_full_lesson_refuses_before_any_charge(self):
        self.course_twice.capacity = 1
        self.course_twice.save()
        other_family = Family.objects.create(name='Levi', phone='0509999999', branch=self.branch)
        other = Child.objects.create(family=other_family, first_name='Ido', last_name='Levi', birth_date=date(2018, 1, 1), gender='male', status='active')
        LessonEnrollment.objects.create(lesson=self.mon, child=other, status='active')
        with patch('apps.enrollments.change_pricing.TranzilaService.production') as prod, self._today():
            res = self.client.post(self.url, {'bundle_id': str(self.bundle.id), 'expected_new_amount': '335.00'}, format='json')
            prod.return_value.charge_with_token.assert_not_called()
        self.assertEqual(res.status_code, 400)
        self.assertIn('מלא', res.data['error'])
        self.assertFalse(Payment.objects.filter(payment_type='one_time').exists())
