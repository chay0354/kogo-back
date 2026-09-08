"""Card link for an existing customer: token, quote, the money rails, and the CRM endpoints."""
from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.signing import dumps
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Business, BusinessCategory, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.card_link import (
    SIGN_SALT,
    CardLinkError,
    apply_card_link,
    build_card_link_token,
    quote_standing_order,
    resolve_card_link_token,
)
from apps.customers.models import Child, Family, Parent, Payment, RecurringPayment, TranzilaTransaction
from apps.enrollments.models import LessonEnrollment
from apps.payment_links.models import CardLink

CARD = {'card_number': '4580000000000000', 'expiry_month': 12, 'expiry_year': 2030, 'cvv': '123', 'card_holder_id': '039876545'}
OK_CHARGE = {'success': True, 'token': 'tok_abc', 'transaction_id': 'T1', 'confirmation_code': 'C1', 'response_code': '000', 'raw_response': {}}


def _user(role, username):
    User = get_user_model()
    user = User.objects.create_user(username=username, email=username, password='x')
    UserProfile.objects.update_or_create(user=user, defaults={'role': role})
    return user


def _client_for(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
    return client


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class _Base(TestCase):
    def setUp(self):
        cache.clear()
        self.branch = Branch.objects.create(name='Main')
        room = Room.objects.create(branch=self.branch, name='A', capacity=20)
        ctype = CourseType.objects.create(name='Dance')
        self.course = Course.objects.create(course_type=ctype, name='Dance', price=Decimal('400.00'), capacity=10, branch=self.branch)
        self.lesson = Lesson.objects.create(
            course=self.course, room=room, day_of_week=0, start_time=time(17, 0), end_time=time(18, 0), is_recurring=True,
        )
        self.family = Family.objects.create(name='Cohen', phone='0501111111', branch=self.branch)
        Parent.objects.create(family=self.family, first_name='Dana', last_name='Cohen', phone='0501111111', is_primary=True)
        self.child = Child.objects.create(
            family=self.family, first_name='Noa', last_name='Cohen', birth_date=date(2016, 1, 1), gender='female', status='active',
        )
        self.manager = _user(UserProfile.ROLE_MANAGER, 'm@test.com')
        self.client = _client_for(self.manager)
        self.business = Business.objects.create(name='עסק בדיקה')
        self.category = BusinessCategory.objects.create(business=self.business, name='קטגוריה')

    def _sto_link(self, **kwargs):
        return CardLink.objects.create(kind=CardLink.KIND_STANDING_ORDER, child=self.child, lesson=self.lesson,
                                       branch=self.branch, created_by=self.manager, **kwargs)

    def _one_time_link(self, amount='150.00', **kwargs):
        return CardLink.objects.create(kind=CardLink.KIND_ONE_TIME, child=self.child, amount=Decimal(amount),
                                       description='חולצה', branch=self.branch, business=self.business,
                                       business_category=self.category, created_by=self.manager, **kwargs)


class TokenTest(_Base):
    def test_round_trip_and_garbage(self):
        link = self._sto_link()
        token = build_card_link_token(link)
        self.assertNotIn(':', token)
        resolved, done = resolve_card_link_token(token)
        self.assertEqual(resolved.id, link.id)
        self.assertFalse(done)
        with self.assertRaises(CardLinkError):
            resolve_card_link_token('garbage')
        with self.assertRaises(CardLinkError):
            resolve_card_link_token('')

    def test_version_bump_invalidates_the_old_link(self):
        link = self._sto_link()
        old = build_card_link_token(link)
        link.token_version += 1
        link.save()
        with self.assertRaises(CardLinkError):
            resolve_card_link_token(old)
        resolve_card_link_token(build_card_link_token(link))

    def test_completed_resolves_as_done_and_cancelled_refuses(self):
        link = self._sto_link(status=CardLink.STATUS_COMPLETED)
        _, done = resolve_card_link_token(build_card_link_token(link))
        self.assertTrue(done)
        link.status = CardLink.STATUS_CANCELLED
        link.save()
        with self.assertRaises(CardLinkError):
            resolve_card_link_token(build_card_link_token(link))

    def test_expired_token(self):
        link = self._sto_link()
        with patch('apps.customers.card_link.CARD_LINK_TOKEN_MAX_AGE', -1):
            with self.assertRaises(CardLinkError):
                resolve_card_link_token(build_card_link_token(link))


class QuoteTest(_Base):
    def test_quote_matches_what_charge_subscription_with_card_would_bill(self):
        """Pin: the link charges exactly what the widget/CRM signup path charges for the same child and lesson."""
        from apps.core.payment_service import PaymentService

        link = self._sto_link()
        today = date(2026, 9, 8)
        quote = quote_standing_order(link, today)
        self.assertEqual(quote['registration_fee'], Decimal('120.00'))
        self.assertGreater(quote['first_charge'], quote['registration_fee'])

        from unittest.mock import MagicMock

        service = PaymentService()
        gateway = MagicMock()
        gateway.charge_with_card.return_value = dict(OK_CHARGE)
        service.tranzila_service = gateway
        with patch.object(service, '_create_invoice_from_payment'), patch.object(service, '_send_registration_whatsapp'):
            result = service.charge_subscription_with_card(
                child_id=str(self.child.id), lesson_id=str(self.lesson.id), payment_date=today, **CARD,
            )
        self.assertTrue(result.get('success'), result)
        charged = gateway.charge_with_card.call_args.kwargs['amount']
        self.assertEqual(charged, quote['first_charge'])
        recurring = RecurringPayment.objects.get(child=self.child)
        self.assertEqual(recurring.amount, quote['monthly_amount'])
        self.assertEqual(recurring.next_billing_date, quote['next_billing_date'])


class StandingOrderRailsTest(_Base):
    def setUp(self):
        super().setUp()
        self.link = self._sto_link()

    def test_success_creates_payment_transaction_standing_order_enrollment(self):
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            prod.return_value.charge_with_card.return_value = dict(OK_CHARGE)
            out = apply_card_link(self.link, CARD)
        self.assertTrue(out['success'])
        self.link.refresh_from_db()
        self.assertEqual(self.link.status, CardLink.STATUS_COMPLETED)
        payment = self.link.payment
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.payment_type, 'recurring_subscription')
        self.assertEqual(payment.lesson_id, self.lesson.id)
        txn = TranzilaTransaction.objects.get(idempotency_key=f'card_link_{self.link.id}_1_1')
        self.assertEqual(payment.tranzila_transaction_id, txn.id)
        recurring = RecurringPayment.objects.get(child=self.child)
        self.assertEqual(recurring.tranzila_token, 'tok_abc')
        self.assertEqual(recurring.card_expire_year, 2030)
        self.assertEqual(recurring.status, 'active')
        self.assertEqual(self.link.recurring_payment_id, recurring.id)
        self.assertTrue(LessonEnrollment.objects.filter(child=self.child, lesson=self.lesson, status='active').exists())
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'active')
        self.assertIsNotNone(self.child.paid_until_date)

    def test_child_already_on_the_roster_without_a_standing_order_is_allowed(self):
        LessonEnrollment.objects.create(child=self.child, lesson=self.lesson, status='active', start_date=date(2026, 9, 1))
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            prod.return_value.charge_with_card.return_value = dict(OK_CHARGE)
            out = apply_card_link(self.link, CARD)
        self.assertTrue(out['success'])
        self.assertEqual(LessonEnrollment.objects.filter(child=self.child, lesson=self.lesson).count(), 1)

    def test_existing_standing_order_is_refused_before_the_gateway(self):
        first = Payment.objects.create(child=self.child, family=self.family, lesson=self.lesson, branch=self.branch,
                                       payment_type='recurring_subscription', status='completed',
                                       base_amount=400, discount_amount=0, final_amount=400)
        RecurringPayment.objects.create(child=self.child, initial_payment=first, tranzila_token='old', status='active',
                                        base_amount=400, discount_amount=0, amount=400, billing_day=1,
                                        start_date=date(2026, 9, 1), next_billing_date=date(2026, 10, 1))
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            with self.assertRaises(CardLinkError):
                apply_card_link(self.link, CARD)
            prod.return_value.charge_with_card.assert_not_called()
        self.link.refresh_from_db()
        self.assertEqual(self.link.status, CardLink.STATUS_PENDING)

    def test_no_token_keeps_the_money_flags_review_and_does_not_enroll(self):
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            prod.return_value.charge_with_card.return_value = {**OK_CHARGE, 'token': ''}
            out = apply_card_link(self.link, CARD)
        self.assertTrue(out['success'])
        self.assertTrue(out['review'])
        self.link.refresh_from_db()
        self.assertEqual(self.link.status, CardLink.STATUS_REVIEW)
        self.assertEqual(self.link.review_reason, 'no_token')
        self.assertEqual(self.link.payment.status, 'completed')
        self.assertFalse(RecurringPayment.objects.filter(child=self.child).exists())
        self.assertFalse(LessonEnrollment.objects.filter(child=self.child, lesson=self.lesson).exists())
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'active')

    def test_decline_puts_the_link_back_so_another_card_can_be_tried(self):
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            prod.return_value.charge_with_card.return_value = {'success': False, 'error': 'declined'}
            with self.assertRaises(CardLinkError):
                apply_card_link(self.link, CARD)
            self.link.refresh_from_db()
            self.assertEqual(self.link.status, CardLink.STATUS_PENDING)
            self.assertEqual(self.link.attempts, 1)
            self.assertEqual(Payment.objects.get(card_link=self.link).status, 'failed')
            prod.return_value.charge_with_card.return_value = dict(OK_CHARGE)
            out = apply_card_link(self.link, CARD)
        self.assertTrue(out['success'])
        self.assertEqual(TranzilaTransaction.objects.filter(idempotency_key__startswith=f'card_link_{self.link.id}_').count(), 1)
        self.assertEqual(self.link.attempts, 1)
        self.link.refresh_from_db()
        self.assertEqual(self.link.attempts, 2)

    def test_a_gateway_exception_freezes_the_link_for_review(self):
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            prod.return_value.charge_with_card.side_effect = RuntimeError('socket closed')
            with self.assertRaises(CardLinkError) as ctx:
                apply_card_link(self.link, CARD)
        self.assertTrue(ctx.exception.processing)
        self.link.refresh_from_db()
        self.assertEqual(self.link.status, CardLink.STATUS_REVIEW)
        self.assertEqual(self.link.review_reason, 'gateway_uncertain')
        # A second submit does not reach the gateway.
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            with self.assertRaises(CardLinkError):
                apply_card_link(CardLink.objects.get(id=self.link.id), CARD)
            prod.return_value.charge_with_card.assert_not_called()

    def test_an_uncertain_gateway_answer_is_review_not_a_decline(self):
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            prod.return_value.charge_with_card.return_value = {'success': False, 'uncertain': True, 'error': 'timeout'}
            with self.assertRaises(CardLinkError) as ctx:
                apply_card_link(self.link, CARD)
        self.assertTrue(ctx.exception.processing)
        self.link.refresh_from_db()
        self.assertEqual(self.link.status, CardLink.STATUS_REVIEW)
        self.assertEqual(Payment.objects.get(card_link=self.link).status, 'pending')

    def test_a_failure_after_the_charge_keeps_the_money_recorded_and_never_releases(self):
        with patch('apps.customers.card_link.TranzilaService.production') as prod, \
             patch('apps.customers.card_link._ensure_recurring_payment_for_widget_charge', side_effect=RuntimeError('db')):
            prod.return_value.charge_with_card.return_value = dict(OK_CHARGE)
            with self.assertRaises(CardLinkError) as ctx:
                apply_card_link(self.link, CARD)
        self.assertTrue(ctx.exception.processing)
        self.link.refresh_from_db()
        self.assertEqual(self.link.status, CardLink.STATUS_REVIEW)
        self.assertEqual(self.link.review_reason, 'record_failed')
        payment = Payment.objects.get(card_link=self.link)
        self.assertEqual(payment.status, 'completed')
        self.assertIsNotNone(payment.tranzila_transaction)
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            with self.assertRaises(CardLinkError):
                apply_card_link(CardLink.objects.get(id=self.link.id), CARD)
            prod.return_value.charge_with_card.assert_not_called()

    def test_a_stale_processing_link_goes_to_review_not_back_to_pending(self):
        CardLink.objects.filter(id=self.link.id).update(status=CardLink.STATUS_PROCESSING, charge_started_at=timezone.now())
        with self.assertRaises(CardLinkError) as ctx:
            apply_card_link(CardLink.objects.get(id=self.link.id), CARD)
        self.assertTrue(ctx.exception.processing)
        CardLink.objects.filter(id=self.link.id).update(charge_started_at=timezone.now() - timedelta(seconds=120))
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            with self.assertRaises(CardLinkError):
                apply_card_link(CardLink.objects.get(id=self.link.id), CARD)
            prod.return_value.charge_with_card.assert_not_called()
        self.link.refresh_from_db()
        self.assertEqual(self.link.status, CardLink.STATUS_REVIEW)
        self.assertEqual(self.link.review_reason, 'stale_processing')

    def test_a_quote_problem_refuses_before_the_claim(self):
        self.course.price = Decimal('0.00')
        self.course.save()
        with self.assertRaises(CardLinkError):
            apply_card_link(self.link, CARD)
        self.link.refresh_from_db()
        self.assertEqual(self.link.status, CardLink.STATUS_PENDING)
        self.assertEqual(self.link.attempts, 0)

    def test_no_standing_order_created_is_review(self):
        with patch('apps.customers.card_link.TranzilaService.production') as prod, \
             patch('apps.customers.card_link._ensure_recurring_payment_for_widget_charge', return_value=False):
            prod.return_value.charge_with_card.return_value = dict(OK_CHARGE)
            out = apply_card_link(self.link, CARD)
        self.assertTrue(out['review'])
        self.link.refresh_from_db()
        self.assertEqual(self.link.review_reason, 'no_standing_order')
        self.assertFalse(LessonEnrollment.objects.filter(child=self.child, lesson=self.lesson).exists())

    def test_attempt_cap(self):
        CardLink.objects.filter(id=self.link.id).update(attempts=6)
        with self.assertRaises(CardLinkError):
            apply_card_link(CardLink.objects.get(id=self.link.id), CARD)

    @override_settings(SUBSCRIPTION_FIRST_CHARGE_DATE='2099-01-01')
    def test_fee_only_signup_charges_the_fee_and_starts_billing_later(self):
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            prod.return_value.charge_with_card.return_value = dict(OK_CHARGE)
            out = apply_card_link(self.link, CARD)
        self.assertEqual(Decimal(out['charged']), Decimal('120.00'))
        self.assertEqual(out['next_billing_date'], '2099-01-01')

    @override_settings(REGISTRATION_FEE_ILS=0, SUBSCRIPTION_FIRST_CHARGE_DATE='2099-01-01')
    def test_zero_first_charge_verifies_the_card_instead(self):
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            prod.return_value.verify_card.return_value = dict(OK_CHARGE)
            out = apply_card_link(self.link, CARD)
        self.assertEqual(Decimal(out['charged']), Decimal('0.00'))
        prod.return_value.charge_with_card.assert_not_called()
        prod.return_value.verify_card.assert_called_once()
        self.assertTrue(RecurringPayment.objects.filter(child=self.child, tranzila_token='tok_abc').exists())

    def test_second_submit_after_completion_is_already_done(self):
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            prod.return_value.charge_with_card.return_value = dict(OK_CHARGE)
            apply_card_link(self.link, CARD)
            with self.assertRaises(CardLinkError) as ctx:
                apply_card_link(CardLink.objects.get(id=self.link.id), CARD)
            self.assertTrue(ctx.exception.already_done)
            self.assertEqual(prod.return_value.charge_with_card.call_count, 1)


class OneTimeRailsTest(_Base):
    def test_one_time_charge_records_a_payment_and_no_standing_order(self):
        link = self._one_time_link()
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            prod.return_value.charge_with_card.return_value = dict(OK_CHARGE)   # token ignored
            with patch('apps.customers.card_link.PaymentService._create_invoice_from_payment') as invoice:
                out = apply_card_link(link, CARD)
        self.assertTrue(out['success'])
        self.assertEqual(Decimal(out['charged']), Decimal('150.00'))
        link.refresh_from_db()
        payment = link.payment
        self.assertEqual(payment.payment_type, 'one_time')
        self.assertIsNone(payment.lesson)
        self.assertEqual(payment.final_amount, Decimal('150.00'))
        self.assertEqual(payment.branch_id, self.branch.id)
        self.assertFalse(RecurringPayment.objects.filter(child=self.child).exists())
        invoice.assert_called_once()
        self.assertEqual(TranzilaTransaction.objects.get(idempotency_key=f'card_link_{link.id}_1_1').transaction_type, 'charge')

    def test_one_time_revenue_is_tagged_by_the_link(self):
        from apps.core.revenue_service import aggregate_income_by_business

        link = self._one_time_link()
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            prod.return_value.charge_with_card.return_value = dict(OK_CHARGE)
            with patch('apps.customers.card_link.PaymentService._create_invoice_from_payment'):
                apply_card_link(link, CARD)
        today = date.today()
        out = aggregate_income_by_business(today - timedelta(days=1), today + timedelta(days=1))
        bucket = next(b for b in out if b['business_id'] == str(self.business.id))
        cat = next(c for c in bucket['categories'] if c['category_id'] == str(self.category.id))
        self.assertEqual(cat['revenue'], 150.0)


class CardLinkApiTest(_Base):
    def test_partner_cannot_create(self):
        partner = _user(UserProfile.ROLE_PARTNER, 'p@test.com')
        partner.profile.assigned_branches.add(self.branch)
        res = _client_for(partner).post('/api/v1/customers/card-links/', {'kind': 'one_time', 'child_id': str(self.child.id), 'amount': '10', 'description': 'x'}, format='json')
        self.assertEqual(res.status_code, 403)

    def test_create_standing_order_link_with_quote(self):
        res = self.client.post('/api/v1/customers/card-links/', {
            'kind': 'standing_order', 'child_id': str(self.child.id), 'lesson_id': str(self.lesson.id),
        }, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertIn('/card-link/', res.data['public_url'])
        self.assertEqual(Decimal(res.data['quote']['registration_fee']), Decimal('120.00'))
        self.assertEqual(res.data['status'], 'pending')

    def test_create_refuses_existing_standing_order_and_bad_one_time(self):
        first = Payment.objects.create(child=self.child, family=self.family, lesson=self.lesson, branch=self.branch,
                                       payment_type='recurring_subscription', status='completed',
                                       base_amount=400, discount_amount=0, final_amount=400)
        RecurringPayment.objects.create(child=self.child, initial_payment=first, tranzila_token='old', status='active',
                                        base_amount=400, discount_amount=0, amount=400, billing_day=1,
                                        start_date=date(2026, 9, 1), next_billing_date=date(2026, 10, 1))
        res = self.client.post('/api/v1/customers/card-links/', {'kind': 'standing_order', 'child_id': str(self.child.id), 'lesson_id': str(self.lesson.id)}, format='json')
        self.assertEqual(res.status_code, 400)
        res = self.client.post('/api/v1/customers/card-links/', {'kind': 'one_time', 'child_id': str(self.child.id), 'amount': '0.5', 'description': 'x'}, format='json')
        self.assertEqual(res.status_code, 400)
        res = self.client.post('/api/v1/customers/card-links/', {'kind': 'one_time', 'child_id': str(self.child.id), 'amount': '20', 'description': ''}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_send_cancel_regenerate(self):
        link = self._one_time_link()
        with patch('apps.customers.card_link.ManyChatService.notify_registration', return_value={'sent': True, 'method': 'flow'}) as send:
            res = self.client.post(f'/api/v1/customers/card-links/{link.id}/send/')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data['whatsapp']['sent'])
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs['kind'], 'card_link')
        self.assertIn('/card-link/', kwargs['extra_fields']['kogo_card_update_url'])
        self.assertEqual(kwargs['extra_fields']['kogo_amount'], '150.00')
        old_token = build_card_link_token(link)
        res = self.client.post(f'/api/v1/customers/card-links/{link.id}/cancel/')
        self.assertEqual(res.data['status'], 'cancelled')
        self.assertEqual(APIClient().get(f'/api/v1/customers/card-link/{old_token}/').status_code, 400)
        res = self.client.post(f'/api/v1/customers/card-links/{link.id}/regenerate/')
        self.assertEqual(res.data['status'], 'pending')
        self.assertEqual(APIClient().get(f"/api/v1/customers/card-link/{res.data['public_url'].split('/card-link/')[1]}/").status_code, 200)

    def test_public_preview_and_charge(self):
        link = self._one_time_link()
        token = build_card_link_token(link)
        public = APIClient()
        res = public.get(f'/api/v1/customers/card-link/{token}/')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['kind'], 'one_time')
        self.assertEqual(res.data['amount'], '150.00')
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            prod.return_value.charge_with_card.return_value = dict(OK_CHARGE)
            with patch('apps.customers.card_link.PaymentService._create_invoice_from_payment'):
                res = public.post(f'/api/v1/customers/card-link/{token}/charge/', {'card_details': CARD}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data['success'])
        res = public.post(f'/api/v1/customers/card-link/{token}/charge/', {'card_details': CARD}, format='json')
        self.assertTrue(res.data['already_done'])
        res = public.post(f'/api/v1/customers/card-link/{token}/charge/', {'card_details': {'card_number': ''}}, format='json')
        self.assertTrue(res.data['already_done'])

    def test_public_charge_is_throttled(self):
        from rest_framework.throttling import ScopedRateThrottle

        link = self._one_time_link()
        token = build_card_link_token(link)
        with patch.object(ScopedRateThrottle, 'THROTTLE_RATES', {'card_link_charge': '1/min', 'card_link_view': '30/min'}):
            public = APIClient()
            public.post(f'/api/v1/customers/card-link/{token}/charge/', {'card_details': {}}, format='json')
            res = public.post(f'/api/v1/customers/card-link/{token}/charge/', {'card_details': {}}, format='json')
        self.assertEqual(res.status_code, 429)

    def test_regenerate_is_refused_while_a_charge_is_in_flight_and_keeps_attempts(self):
        link = self._one_time_link()
        CardLink.objects.filter(id=link.id).update(status=CardLink.STATUS_PROCESSING, charge_started_at=timezone.now(), attempts=2)
        res = self.client.post(f'/api/v1/customers/card-links/{link.id}/regenerate/')
        self.assertEqual(res.status_code, 409)
        CardLink.objects.filter(id=link.id).update(status=CardLink.STATUS_PENDING)
        res = self.client.post(f'/api/v1/customers/card-links/{link.id}/regenerate/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['attempts'], 2)
