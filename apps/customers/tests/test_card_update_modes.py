"""
The two card modes on a standing order: `renew` and `card_only`.

Money rules under test, in the order they matter:
  - renew charges the exact months that were never collected, each at the full
    monthly amount, never prorated;
  - card_only charges nothing at all, even with a month outstanding;
  - a month this link paid is never charged again by the monthly run;
  - the mode and the amount ride inside the signature, so the URL cannot be
    edited into a different charge.
"""
from datetime import date, timedelta
from decimal import Decimal
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.core.payment_service import JERUSALEM_TZ
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.card_update import (
    MODE_CARD_ONLY,
    MODE_RENEW,
    CardUpdateError,
    apply_new_card,
    build_card_update_token,
    missed_months,
    plan_renew_charge,
    preview_payload,
    renew_quote,
    resolve_card_update_intent,
)
from apps.customers.models import Payment, RecurringPayment, TranzilaTransaction
from apps.customers.recurring_billing import process_due_recurring_charges


CARD = {
    'card_number': '4580458045804580',
    'expiry_month': 12,
    'expiry_year': 2028,
    'cvv': '123',
    'card_holder_id': '123456782',
}

TRANZILA_OK = {
    'success': True,
    'transaction_id': 'cu-1',
    'confirmation_code': 'AUTHCU',
    'token': 'Ynewtoken4580',
    'amount': 250.0,
    'response_code': '000',
    'raw_response': {'transaction_result': {'token': 'Ynewtoken4580'}},
}

TRANZILA_DECLINED = {'success': False, 'error': 'הכרטיס נדחה', 'response_code': '004'}

TRANZILA_SETTINGS = dict(
    CRM_FRONTEND_URL='https://kogo-front.vercel.app',
    TRANZILA_TERMINAL='test_terminal',
    TRANZILA_PUBLIC_KEY='test_public_key',
    TRANZILA_SECRET_KEY='test_secret_key',
    TRANZILA_PROD_TERMINAL='test_terminal',
    TRANZILA_PROD_TOKEN_TERMINAL='test_terminal',
    TRANZILA_PROD_PUBLIC_KEY='test_public_key',
    TRANZILA_PROD_SECRET_KEY='test_secret_key',
)


def today() -> date:
    return timezone.now().astimezone(JERUSALEM_TZ).date()


def month_start(offset: int = 0) -> date:
    """First of the month `offset` months from the current one (negative = past)."""
    now = today()
    total = now.year * 12 + (now.month - 1) + offset
    return date(total // 12, total % 12 + 1, 1)


def make_sto(*, amount='250.00', status='failed', next_billing=None, last_charge=None, token='oldtoken'):
    family = TestDataFactory.create_family()
    TestDataFactory.create_parent(family=family)
    child = TestDataFactory.create_child(family=family)
    lesson = TestDataFactory.create_lesson()
    initial = Payment.objects.create(
        child=child,
        family=family,
        parent=family.parents.first(),
        lesson=lesson,
        branch=lesson.course.branch,
        payment_type='recurring_subscription',
        status='completed',
        base_amount=Decimal(amount),
        discount_amount=Decimal('0.00'),
        final_amount=Decimal(amount),
        registration_fee=Decimal('0.00'),
        description='מנוי',
    )
    return RecurringPayment.objects.create(
        child=child,
        initial_payment=initial,
        tranzila_token=token,
        status=status,
        base_amount=Decimal(amount),
        amount=Decimal(amount),
        billing_day=1,
        start_date=month_start(-6),
        next_billing_date=next_billing if next_billing is not None else month_start(0),
        last_charge_date=last_charge if last_charge is not None else month_start(-1) + timedelta(days=3),
        card_expire_month=8,
        card_expire_year=2026,
    )


# ---------------------------------------------------------------------------
# What was not collected
# ---------------------------------------------------------------------------

class MissedMonthsTests(TestCase):
    def test_one_missed_month_is_one_full_monthly_amount(self):
        recurring = make_sto(amount='250.00', next_billing=month_start(0), last_charge=month_start(-1) + timedelta(days=3))
        quote = renew_quote(recurring)
        self.assertEqual(quote['months'], [month_start(0)])
        self.assertEqual(quote['amount'], Decimal('250.00'))

    def test_three_missed_months_are_three_full_monthly_amounts(self):
        recurring = make_sto(
            amount='250.00', next_billing=month_start(-2), last_charge=month_start(-3) + timedelta(days=5),
        )
        quote = renew_quote(recurring)
        self.assertEqual(quote['months'], [month_start(-2), month_start(-1), month_start(0)])
        # Three whole months, never a prorated part-month and no registration fee.
        self.assertEqual(quote['amount'], Decimal('750.00'))

    def test_nothing_outstanding_when_the_next_charge_is_still_ahead(self):
        recurring = make_sto(next_billing=month_start(1), last_charge=month_start(0) + timedelta(days=2))
        self.assertEqual(renew_quote(recurring)['months'], [])
        self.assertEqual(renew_quote(recurring)['amount'], Decimal('0.00'))

    def test_a_month_with_a_completed_subscription_payment_is_not_missed(self):
        recurring = make_sto(next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1))
        self.assertEqual(missed_months(recurring), [month_start(-1), month_start(0)])
        Payment.objects.create(
            child=recurring.child,
            family=recurring.child.family,
            lesson=recurring.initial_payment.lesson,
            branch=recurring.initial_payment.lesson.course.branch,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('250.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('250.00'),
            description='מנוי',
            payment_date=timezone.now() - timedelta(days=(today() - month_start(-1)).days),
        )
        self.assertEqual(missed_months(recurring), [month_start(0)])

    def test_a_payment_just_after_midnight_counts_in_its_own_israeli_month(self):
        """00:30 on the 1st is still UTC on the 31st — it must not read as last month."""
        from datetime import datetime, time as dt_time

        recurring = make_sto(next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1))
        just_after_midnight = timezone.make_aware(
            datetime.combine(month_start(0), dt_time(0, 30)), JERUSALEM_TZ,
        )
        Payment.objects.create(
            child=recurring.child,
            family=recurring.child.family,
            lesson=recurring.initial_payment.lesson,
            branch=recurring.initial_payment.lesson.course.branch,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('250.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('250.00'),
            description='מנוי',
            payment_date=just_after_midnight,
        )
        # The current month is collected; only the one before it is still open.
        self.assertEqual(missed_months(recurring), [month_start(-1)])

    def test_last_charge_date_inside_a_month_settles_it(self):
        recurring = make_sto(next_billing=month_start(-1), last_charge=month_start(-1) + timedelta(days=4))
        self.assertEqual(missed_months(recurring), [month_start(0)])

    def test_plan_drops_a_month_collected_since_the_link_was_made(self):
        """The office saw two months; the cron took the older one in between."""
        recurring = make_sto(next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1))
        wanted = [month_start(-1), month_start(0)]
        # The cron settled the first month: next_billing_date moved on.
        recurring.next_billing_date = month_start(0)
        recurring.last_charge_date = month_start(-1) + timedelta(days=6)
        recurring.save(update_fields=['next_billing_date', 'last_charge_date', 'updated_at'])
        plan = plan_renew_charge(recurring, months=wanted, amount=Decimal('500.00'))
        self.assertEqual(plan['settle'], [month_start(0)])
        self.assertEqual(plan['amount'], Decimal('250.00'))

    def test_plan_settles_nothing_once_every_month_is_covered(self):
        recurring = make_sto(next_billing=month_start(1), last_charge=month_start(0) + timedelta(days=1))
        plan = plan_renew_charge(recurring, months=[month_start(-1), month_start(0)], amount=Decimal('500.00'))
        self.assertEqual(plan['settle'], [])
        self.assertEqual(plan['amount'], Decimal('0.00'))


# ---------------------------------------------------------------------------
# The token carries the intent
# ---------------------------------------------------------------------------

class CardUpdateTokenModeTests(TestCase):
    def test_renew_token_carries_mode_amount_and_months(self):
        recurring = make_sto(next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1))
        months = [month_start(-1), month_start(0)]
        token = build_card_update_token(recurring, mode=MODE_RENEW, amount=Decimal('500.00'), months=months)
        intent = resolve_card_update_intent(token)
        self.assertEqual(intent.mode, MODE_RENEW)
        self.assertEqual(intent.amount, Decimal('500.00'))
        self.assertEqual(intent.months, months)
        self.assertFalse(intent.already_done)

    def test_card_only_token_carries_mode_and_no_amount(self):
        recurring = make_sto(status='active')
        intent = resolve_card_update_intent(build_card_update_token(recurring, mode=MODE_CARD_ONLY))
        self.assertEqual(intent.mode, MODE_CARD_ONLY)
        self.assertIsNone(intent.amount)
        self.assertEqual(intent.months, [])

    def test_a_token_with_no_mode_behaves_exactly_as_before(self):
        recurring = make_sto()
        intent = resolve_card_update_intent(build_card_update_token(recurring))
        self.assertEqual(intent.mode, '')
        self.assertIsNone(intent.amount)
        self.assertEqual(intent.months, [])

    def test_a_tampered_amount_is_refused(self):
        recurring = make_sto(next_billing=month_start(0))
        token = build_card_update_token(
            recurring, mode=MODE_RENEW, amount=Decimal('250.00'), months=[month_start(0)],
        )
        # One character of the signed payload changed — nothing may resolve.
        body = token.split('~')[0]
        tampered = token.replace(body, body[:-1] + ('A' if body[-1] != 'A' else 'B'), 1)
        with self.assertRaises(CardUpdateError):
            resolve_card_update_intent(tampered)

    def test_nothing_appended_to_the_url_can_raise_the_amount(self):
        recurring = make_sto(next_billing=month_start(0))
        token = build_card_update_token(
            recurring, mode=MODE_RENEW, amount=Decimal('250.00'), months=[month_start(0)],
        )
        # The amount is not a parameter to override; anything added to the token
        # breaks the signature, and the signed 250 is the only figure there is.
        for edit in (f'{token}?amount=9999', f'{token}&a=9999', f'{token}9999'):
            with self.assertRaises(CardUpdateError):
                resolve_card_update_intent(edit)
        self.assertEqual(resolve_card_update_intent(token).amount, Decimal('250.00'))

    def test_an_unknown_signed_mode_is_refused_rather_than_guessed(self):
        recurring = make_sto()
        from django.core.signing import dumps

        from apps.customers.card_update import SIGN_SALT, _stamp

        forged = dumps(
            {'id': str(recurring.id), 'v': _stamp(recurring), 'm': 'charge_everything'}, salt=SIGN_SALT,
        ).replace(':', '~')
        with self.assertRaises(CardUpdateError):
            resolve_card_update_intent(forged)


# ---------------------------------------------------------------------------
# renew — the charge, the dates, and the monthly run afterwards
# ---------------------------------------------------------------------------

@override_settings(**TRANZILA_SETTINGS)
class RenewChargeTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _renew_token(self, recurring, amount=None):
        quote = renew_quote(recurring)
        return build_card_update_token(
            recurring,
            mode=MODE_RENEW,
            amount=quote['amount'] if amount is None else Decimal(amount),
            months=quote['months'],
        ), quote

    @patch('apps.customers.card_update.PaymentService._create_invoice_from_payment')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_two_missed_months_charge_twice_the_monthly_amount_once(self, mock_charge, mock_receipt):
        recurring = make_sto(
            amount='250.00', next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        token, quote = self._renew_token(recurring)
        self.assertEqual(quote['amount'], Decimal('500.00'))

        res = self.client.post(
            f'/api/v1/customers/card-update/{token}/charge/', {'card_details': CARD}, format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data['charged'])
        self.assertEqual(res.data['amount'], '500.00')
        self.assertEqual(res.data['months'], [f'{month_start(-1):%Y-%m}', f'{month_start(0):%Y-%m}'])

        # One call, one Payment for the total, one receipt.
        self.assertEqual(mock_charge.call_count, 1)
        self.assertEqual(mock_charge.call_args.kwargs['amount'], Decimal('500.00'))
        payments = Payment.objects.filter(child=recurring.child, status='completed').exclude(
            id=recurring.initial_payment_id,
        )
        self.assertEqual(payments.count(), 1)
        payment = payments.get()
        self.assertEqual(payment.final_amount, Decimal('500.00'))
        self.assertIn('חידוש הוראת קבע', payment.description)
        self.assertIn('עבור', payment.description)
        self.assertEqual(mock_receipt.call_count, 1)

        recurring.refresh_from_db()
        self.assertEqual(recurring.status, 'active')
        self.assertEqual(recurring.tranzila_token, 'Ynewtoken4580')
        self.assertEqual(recurring.card_expire_month, 12)
        # Past *every* month just paid.
        self.assertEqual(recurring.next_billing_date, month_start(1))
        self.assertEqual(recurring.last_charge_date, today())
        recurring.child.refresh_from_db()
        self.assertEqual(recurring.child.status, 'active')
        self.assertEqual(recurring.child.paid_until_date, month_start(1) - timedelta(days=1))

    @patch('apps.customers.card_update.PaymentService._create_invoice_from_payment')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_three_missed_months_charge_three_full_months(self, mock_charge, _receipt):
        recurring = make_sto(
            amount='300.00', next_billing=month_start(-2), last_charge=month_start(-3) + timedelta(days=2),
        )
        token, quote = self._renew_token(recurring)
        self.assertEqual(quote['amount'], Decimal('900.00'))
        apply_new_card(recurring, CARD, intent=resolve_card_update_intent(token))
        self.assertEqual(mock_charge.call_args.kwargs['amount'], Decimal('900.00'))
        recurring.refresh_from_db()
        self.assertEqual(recurring.next_billing_date, month_start(1))

    @patch('apps.customers.card_update.PaymentService._create_invoice_from_payment')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_the_office_override_is_what_the_card_is_charged(self, mock_charge, _receipt):
        recurring = make_sto(
            amount='250.00', next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        token, _ = self._renew_token(recurring, amount='400.00')
        apply_new_card(recurring, CARD, intent=resolve_card_update_intent(token))
        self.assertEqual(mock_charge.call_args.kwargs['amount'], Decimal('400.00'))
        recurring.refresh_from_db()
        # The override changes the money, never which months are settled.
        self.assertEqual(recurring.next_billing_date, month_start(1))

    @patch('apps.customers.card_update.PaymentService._create_invoice_from_payment')
    @patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=TRANZILA_OK)
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_a_second_submit_of_the_same_renew_link_charges_nothing(self, mock_charge, mock_verify, _receipt):
        recurring = make_sto(
            amount='250.00', next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        token, _ = self._renew_token(recurring)
        apply_new_card(recurring, CARD, intent=resolve_card_update_intent(token))
        self.assertEqual(mock_charge.call_count, 1)

        # The same signed link, replayed against the standing order it just fixed.
        recurring.refresh_from_db()
        result = apply_new_card(recurring, CARD, intent=resolve_card_update_intent(token))
        self.assertFalse(result['charged'])
        self.assertEqual(mock_charge.call_count, 1)
        self.assertEqual(
            Payment.objects.filter(child=recurring.child, status='completed')
            .exclude(id=recurring.initial_payment_id).count(),
            1,
        )
        recurring.refresh_from_db()
        self.assertEqual(recurring.next_billing_date, month_start(1))

    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card')
    @patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=TRANZILA_OK)
    def test_a_card_only_link_survives_the_standing_order_being_billed(self, mock_verify, mock_charge):
        """The monthly run touching the row must not quietly kill the office's link."""
        recurring = make_sto(status='active', next_billing=month_start(1))
        token = build_card_update_token(recurring, mode=MODE_CARD_ONLY)
        # Anything that saves the row moves the stamp an old link was pinned to.
        recurring.last_charge_date = today()
        recurring.save(update_fields=['last_charge_date', 'updated_at'])

        intent = resolve_card_update_intent(token)
        self.assertFalse(intent.already_done)
        result = apply_new_card(intent.recurring, CARD, intent=intent)
        self.assertFalse(result['charged'])
        mock_charge.assert_not_called()
        recurring.refresh_from_db()
        self.assertEqual(recurring.tranzila_token, 'Ynewtoken4580')

    def test_a_card_update_link_does_not_expire_with_age(self):
        # Signed a year ago and still good: nothing about a card-update link is
        # closed by the clock. What closes one is the standing order being
        # cancelled, the stamp (mode-less links), or the months being settled.
        recurring = make_sto(next_billing=month_start(-1))
        # Sign the token as it would have been signed 400 days ago.
        with patch('django.core.signing.time.time', return_value=time.time() - 400 * 24 * 3600):
            token, _ = self._renew_token(recurring)
        intent = resolve_card_update_intent(token)
        self.assertTrue(intent.is_renew)

    def test_a_link_with_no_mode_still_expires_on_the_old_stamp(self):
        recurring = make_sto(status='failed', next_billing=month_start(0))
        token = build_card_update_token(recurring)
        recurring.status = 'active'
        recurring.save(update_fields=['status', 'updated_at'])
        self.assertTrue(resolve_card_update_intent(token).already_done)

    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_DECLINED)
    def test_a_declined_card_leaves_the_months_open_for_another_card(self, mock_charge):
        recurring = make_sto(
            amount='250.00', next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        token, _ = self._renew_token(recurring)
        with self.assertRaises(CardUpdateError):
            apply_new_card(recurring, CARD, intent=resolve_card_update_intent(token))
        # No claim is left behind, so a second card may be tried on the same link.
        self.assertFalse(TranzilaTransaction.objects.filter(idempotency_key__startswith='card_update_renew_').exists())
        recurring.refresh_from_db()
        self.assertEqual(recurring.next_billing_date, month_start(-1))

    @patch('apps.customers.card_update.PaymentService._create_invoice_from_payment')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_a_claim_already_in_flight_blocks_a_second_charge(self, mock_charge, _receipt):
        recurring = make_sto(
            amount='250.00', next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        token, _ = self._renew_token(recurring)
        intent = resolve_card_update_intent(token)
        TranzilaTransaction.objects.create(
            transaction_id='', confirmation_code='', transaction_type='recurring_charge',
            response_code='', response_message='', request_data={}, response_data={},
            idempotency_key=(
                f'card_update_renew_{recurring.id}_{month_start(-1):%Y-%m}_{month_start(0):%Y-%m}'
            ),
            is_successful=False,
        )
        with self.assertRaises(CardUpdateError):
            apply_new_card(recurring, CARD, intent=intent)
        mock_charge.assert_not_called()

    @patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=TRANZILA_OK)
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card')
    def test_months_already_collected_keep_the_new_card_and_take_nothing(self, mock_charge, mock_verify):
        recurring = make_sto(
            amount='250.00', next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        token, _ = self._renew_token(recurring)
        # A successful claim for exactly these months already exists.
        TranzilaTransaction.objects.create(
            transaction_id='paid', confirmation_code='A1', transaction_type='recurring_charge',
            response_code='000', response_message='', request_data={}, response_data={},
            idempotency_key=(
                f'card_update_renew_{recurring.id}_{month_start(-1):%Y-%m}_{month_start(0):%Y-%m}'
            ),
            is_successful=True,
        )
        result = apply_new_card(recurring, CARD, intent=resolve_card_update_intent(token))
        self.assertFalse(result['charged'])
        mock_charge.assert_not_called()
        mock_verify.assert_called_once()
        recurring.refresh_from_db()
        self.assertEqual(recurring.tranzila_token, 'Ynewtoken4580')
        # Nothing about the billing dates moved: no month was settled here.
        self.assertEqual(recurring.next_billing_date, month_start(-1))

    @patch('apps.customers.card_update.PaymentService._create_invoice_from_payment')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_a_renew_link_for_a_cancelled_standing_order_is_refused(self, mock_charge, _receipt):
        recurring = make_sto(next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1))
        token, _ = self._renew_token(recurring)
        recurring.status = 'cancelled'
        recurring.save(update_fields=['status', 'updated_at'])
        res = self.client.get(f'/api/v1/customers/card-update/{token}/')
        self.assertEqual(res.status_code, 400)
        res = self.client.post(
            f'/api/v1/customers/card-update/{token}/charge/', {'card_details': CARD}, format='json',
        )
        self.assertEqual(res.status_code, 400)
        mock_charge.assert_not_called()


# ---------------------------------------------------------------------------
# card_only — the card changes, nothing is charged
# ---------------------------------------------------------------------------

@override_settings(**TRANZILA_SETTINGS)
class CardOnlyTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card')
    @patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=TRANZILA_OK)
    def test_charges_nothing_with_a_month_outstanding(self, mock_verify, mock_charge):
        recurring = make_sto(
            status='active', amount='250.00',
            next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        self.assertEqual(renew_quote(recurring)['months'], [month_start(-1), month_start(0)])
        token = build_card_update_token(recurring, mode=MODE_CARD_ONLY)

        res = self.client.post(
            f'/api/v1/customers/card-update/{token}/charge/', {'card_details': CARD}, format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(res.data['charged'])
        self.assertEqual(res.data['amount'], '0')
        mock_charge.assert_not_called()
        mock_verify.assert_called_once()

        recurring.refresh_from_db()
        self.assertEqual(recurring.tranzila_token, 'Ynewtoken4580')
        self.assertEqual(recurring.status, 'active')
        # Untouched, so the monthly run still collects what it should.
        self.assertEqual(recurring.next_billing_date, month_start(-1))
        self.assertEqual(recurring.last_charge_date, month_start(-2) + timedelta(days=1))
        self.assertFalse(
            Payment.objects.filter(child=recurring.child).exclude(id=recurring.initial_payment_id).exists()
        )

    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card')
    @patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=TRANZILA_OK)
    def test_reactivates_a_stopped_order_without_charging(self, mock_verify, mock_charge):
        recurring = make_sto(status='failed', next_billing=month_start(0))
        recurring.child.status = 'payment_problem'
        recurring.child.save(update_fields=['status'])
        token = build_card_update_token(recurring, mode=MODE_CARD_ONLY)
        result = apply_new_card(recurring, CARD, intent=resolve_card_update_intent(token))
        self.assertFalse(result['charged'])
        mock_charge.assert_not_called()
        recurring.refresh_from_db()
        recurring.child.refresh_from_db()
        self.assertEqual(recurring.status, 'active')
        self.assertEqual(recurring.child.status, 'active')


# ---------------------------------------------------------------------------
# The monthly run afterwards
# ---------------------------------------------------------------------------

@override_settings(**TRANZILA_SETTINGS)
class MonthlyRunAfterCardLinkTests(TestCase):
    @patch('apps.core.payment_service.PaymentService._create_invoice_from_payment')
    @patch('apps.customers.card_update.PaymentService._create_invoice_from_payment')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_token')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_the_monthly_run_never_recharges_a_month_the_renewal_paid(
        self, mock_card, mock_token, _receipt_a, _receipt_b,
    ):
        """Renew pays the two open months; the run that follows must do nothing."""
        recurring = make_sto(
            amount='250.00', status='failed',
            next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        quote = renew_quote(recurring)
        self.assertEqual(quote['months'], [month_start(-1), month_start(0)])
        token = build_card_update_token(
            recurring, mode=MODE_RENEW, amount=quote['amount'], months=quote['months'],
        )
        apply_new_card(recurring, CARD, intent=resolve_card_update_intent(token))

        before = Payment.objects.count()
        summary = process_due_recurring_charges()

        mock_token.assert_not_called()
        self.assertEqual(Payment.objects.count(), before)
        self.assertEqual(summary['charged'], 0)
        recurring.refresh_from_db()
        self.assertEqual(recurring.next_billing_date, month_start(1))

    @patch('apps.core.payment_service.PaymentService._create_invoice_from_payment')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_token', return_value=TRANZILA_OK)
    @patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=TRANZILA_OK)
    def test_after_card_only_the_monthly_run_still_collects_on_its_own_date(
        self, mock_verify, mock_token, _receipt,
    ):
        recurring = make_sto(
            amount='250.00', status='active',
            next_billing=month_start(0), last_charge=month_start(-1) + timedelta(days=1),
        )
        token = build_card_update_token(recurring, mode=MODE_CARD_ONLY)
        apply_new_card(recurring, CARD, intent=resolve_card_update_intent(token))

        summary = process_due_recurring_charges()
        self.assertEqual(summary['charged'], 1)
        mock_token.assert_called_once()
        self.assertEqual(mock_token.call_args.kwargs['amount'], Decimal('250.00'))
        # The new card is what it billed.
        self.assertEqual(mock_token.call_args.kwargs['token'], 'Ynewtoken4580')
        recurring.refresh_from_db()
        self.assertEqual(recurring.next_billing_date, month_start(1))


# ---------------------------------------------------------------------------
# The office endpoint: a link to copy, never a send
# ---------------------------------------------------------------------------

@override_settings(**TRANZILA_SETTINGS)
class CardUpdateLinkEndpointTests(TestCase):
    def setUp(self):
        self.recurring = make_sto(
            amount='250.00', next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        self.url = f'/api/v1/customers/recurring-payments/{self.recurring.id}/card-update-link/'

    def _client(self, role):
        User = get_user_model()
        user = User.objects.create_user(
            username=f'{role}-cul@test.com', email=f'{role}-cul@test.com', password='pass12345!', is_active=True,
        )
        UserProfile.objects.update_or_create(user=user, defaults={'role': role})
        if role == UserProfile.ROLE_PARTNER:
            user.profile.assigned_branches.add(self.recurring.initial_payment.lesson.course.branch)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
        return client

    def test_manager_gets_a_renew_url_with_the_months_and_the_exact_total(self):
        res = self._client(UserProfile.ROLE_MANAGER).post(self.url, {'mode': 'renew'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['mode'], 'renew')
        self.assertEqual(res.data['amount'], '500.00')
        self.assertEqual([row['month'] for row in res.data['months']],
                         [f'{month_start(-1):%Y-%m}', f'{month_start(0):%Y-%m}'])
        self.assertEqual(res.data['child_name'], self.recurring.child.full_name)
        self.assertIn('/update-card/', res.data['url'])
        # Nothing about the charge is in the URL itself — it is all signed in.
        self.assertNotIn('mode=', res.data['url'])
        self.assertNotIn('amount=', res.data['url'])
        token = res.data['url'].rsplit('/', 1)[-1]
        intent = resolve_card_update_intent(token)
        self.assertEqual(intent.mode, MODE_RENEW)
        self.assertEqual(intent.amount, Decimal('500.00'))

    def test_the_office_may_override_the_amount(self):
        res = self._client(UserProfile.ROLE_MANAGER).post(
            self.url, {'mode': 'renew', 'amount': '380'}, format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['amount'], '380.00')
        intent = resolve_card_update_intent(res.data['url'].rsplit('/', 1)[-1])
        self.assertEqual(intent.amount, Decimal('380.00'))
        self.assertEqual(len(intent.months), 2)

    def test_an_impossible_override_is_refused(self):
        client = self._client(UserProfile.ROLE_MANAGER)
        for bad in ('0', '-10', '90000', 'abc'):
            res = client.post(self.url, {'mode': 'renew', 'amount': bad}, format='json')
            self.assertEqual(res.status_code, 400, f'{bad}: {res.content}')

    def test_renew_is_refused_when_nothing_is_outstanding(self):
        recurring = make_sto(next_billing=month_start(1), last_charge=month_start(0) + timedelta(days=1))
        res = self._client(UserProfile.ROLE_MANAGER).post(
            f'/api/v1/customers/recurring-payments/{recurring.id}/card-update-link/',
            {'mode': 'renew'}, format='json',
        )
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn('אין חודשים פתוחים', res.data['error'])

    def test_card_only_needs_no_outstanding_month(self):
        recurring = make_sto(status='active', next_billing=month_start(1), last_charge=month_start(0))
        res = self._client(UserProfile.ROLE_MANAGER).post(
            f'/api/v1/customers/recurring-payments/{recurring.id}/card-update-link/',
            {'mode': 'card_only'}, format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['mode'], 'card_only')
        self.assertEqual(res.data['months'], [])
        intent = resolve_card_update_intent(res.data['url'].rsplit('/', 1)[-1])
        self.assertEqual(intent.mode, MODE_CARD_ONLY)

    def test_a_cancelled_standing_order_is_refused(self):
        self.recurring.status = 'cancelled'
        self.recurring.save(update_fields=['status', 'updated_at'])
        client = self._client(UserProfile.ROLE_MANAGER)
        for mode in ('renew', 'card_only'):
            res = client.post(self.url, {'mode': mode}, format='json')
            self.assertEqual(res.status_code, 400, res.content)

    def test_a_standing_order_with_no_lesson_is_refused(self):
        recurring = make_sto(status='active')
        recurring.initial_payment.lesson = None
        recurring.initial_payment.save(update_fields=['lesson'])
        res = self._client(UserProfile.ROLE_MANAGER).post(
            f'/api/v1/customers/recurring-payments/{recurring.id}/card-update-link/',
            {'mode': 'card_only'}, format='json',
        )
        self.assertEqual(res.status_code, 400, res.content)

    def test_an_unknown_mode_is_refused(self):
        res = self._client(UserProfile.ROLE_MANAGER).post(self.url, {'mode': 'free_money'}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_a_partner_may_create_the_link(self):
        res = self._client(UserProfile.ROLE_PARTNER).post(self.url, {'mode': 'card_only'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)

    def test_a_worker_may_not(self):
        res = self._client(UserProfile.ROLE_WORKER).post(self.url, {'mode': 'card_only'}, format='json')
        self.assertEqual(res.status_code, 403, res.content)

    def test_anonymous_may_not(self):
        res = APIClient().post(self.url, {'mode': 'card_only'}, format='json')
        self.assertIn(res.status_code, (401, 403))

    @patch('apps.customers.card_update.send_card_update_whatsapp', return_value={'sent': True})
    def test_the_whatsapp_action_is_untouched(self, mock_send):
        res = self._client(UserProfile.ROLE_MANAGER).post(
            f'/api/v1/customers/recurring-payments/{self.recurring.id}/send-card-update/', {}, format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        mock_send.assert_called_once()


# ---------------------------------------------------------------------------
# What the parent's page says before a digit is typed
# ---------------------------------------------------------------------------

@override_settings(**TRANZILA_SETTINGS)
class PublicPageWordingTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _preview(self, token):
        res = self.client.get(f'/api/v1/customers/card-update/{token}/')
        self.assertEqual(res.status_code, 200, res.content)
        return res.data

    def test_renew_says_the_sum_and_the_month_names(self):
        recurring = make_sto(
            amount='250.00', next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        quote = renew_quote(recurring)
        token = build_card_update_token(
            recurring, mode=MODE_RENEW, amount=quote['amount'], months=quote['months'],
        )
        data = self._preview(token)
        self.assertEqual(data['mode'], 'renew')
        self.assertTrue(data['will_charge'])
        self.assertEqual(data['charge_amount_label'], '500')
        self.assertEqual(len(data['months']), 2)
        self.assertTrue(data['headline'].startswith('יחויב ₪500 — חידוש הוראת קבע עבור '))
        for row in quote['months']:
            from apps.customers.card_update import HEBREW_MONTHS

            self.assertIn(HEBREW_MONTHS[row.month - 1], data['headline'])

    def test_card_only_says_plainly_that_nothing_is_charged(self):
        recurring = make_sto(
            status='active', next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        data = self._preview(build_card_update_token(recurring, mode=MODE_CARD_ONLY))
        self.assertEqual(data['mode'], 'card_only')
        self.assertFalse(data['will_charge'])
        self.assertEqual(data['charge_amount_label'], '0')
        self.assertEqual(data['months'], [])
        self.assertEqual(data['headline'], 'עדכון פרטי אשראי בלבד — לא יבוצע חיוב')

    def test_a_link_with_no_mode_still_says_what_the_old_one_said(self):
        recurring = make_sto(amount='250.00', next_billing=month_start(0))
        data = self._preview(build_card_update_token(recurring))
        self.assertEqual(data['mode'], '')
        self.assertTrue(data['will_charge'])
        self.assertEqual(data['charge_amount_label'], '250')
        self.assertIn('יחויב ₪250', data['headline'])

    @patch('apps.customers.card_update.PaymentService._create_invoice_from_payment')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_after_success_it_says_what_happened(self, _charge, _receipt):
        recurring = make_sto(
            amount='250.00', next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        quote = renew_quote(recurring)
        token = build_card_update_token(
            recurring, mode=MODE_RENEW, amount=quote['amount'], months=quote['months'],
        )
        res = self.client.post(
            f'/api/v1/customers/card-update/{token}/charge/', {'card_details': CARD}, format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn('שולם ₪500 עבור', res.data['message'])

    @patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=TRANZILA_OK)
    def test_after_a_card_only_success_it_says_nothing_was_charged(self, _verify):
        recurring = make_sto(status='active', next_billing=month_start(1))
        token = build_card_update_token(recurring, mode=MODE_CARD_ONLY)
        res = self.client.post(
            f'/api/v1/customers/card-update/{token}/charge/', {'card_details': CARD}, format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['message'], 'פרטי האשראי עודכנו. לא בוצע חיוב.')


# ---------------------------------------------------------------------------
# The popup's options carry the standing order, so it is no longer a dead end
# ---------------------------------------------------------------------------

class CardLinkOptionsStandingOrderTests(TestCase):
    def test_an_option_with_a_standing_order_carries_its_renewal_quote(self):
        from apps.customers.card_link import card_link_options
        from apps.enrollments.models import LessonEnrollment

        recurring = make_sto(
            status='active', amount='250.00',
            next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        lesson = recurring.initial_payment.lesson
        LessonEnrollment.objects.create(child=recurring.child, lesson=lesson, status='active')

        options = card_link_options(recurring.child)
        row = next(o for o in options if o['lesson_id'] == str(lesson.id))
        self.assertTrue(row['has_standing_order'])
        sto = row['standing_order']
        self.assertEqual(sto['id'], str(recurring.id))
        self.assertEqual(sto['status'], 'active')
        self.assertEqual(sto['renew_amount'], '500.00')
        self.assertTrue(sto['can_renew'])
        self.assertEqual(len(sto['months']), 2)

    def test_a_stopped_standing_order_is_offered_for_renewal_too(self):
        from apps.customers.card_link import card_link_options
        from apps.enrollments.models import LessonEnrollment

        recurring = make_sto(status='failed', next_billing=month_start(0))
        lesson = recurring.initial_payment.lesson
        LessonEnrollment.objects.create(child=recurring.child, lesson=lesson, status='active')
        row = next(
            o for o in card_link_options(recurring.child) if o['lesson_id'] == str(lesson.id)
        )
        # `has_standing_order` only knows active/paused; the renewal path does not.
        self.assertFalse(row['has_standing_order'])
        self.assertEqual(row['standing_order']['id'], str(recurring.id))
        self.assertTrue(row['standing_order']['can_renew'])

    def test_a_cancelled_standing_order_is_not_offered(self):
        from apps.customers.card_link import card_link_options
        from apps.enrollments.models import LessonEnrollment

        recurring = make_sto(status='cancelled')
        lesson = recurring.initial_payment.lesson
        LessonEnrollment.objects.create(child=recurring.child, lesson=lesson, status='active')
        row = next(
            o for o in card_link_options(recurring.child) if o['lesson_id'] == str(lesson.id)
        )
        self.assertNotIn('standing_order', row)
