"""Round two of the money review: what a blocked tenancy says, what a run leaves behind,
and the shapes of an answer that must never read as a clean decline.

The verifier's probes (scratchpad/probes2) are here the other way round: each failed
before the fix and passes after it.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.tranzila_service import TranzilaService
from apps.customers.models import CronHeartbeat
from apps.rental_billing.billing import (
    OUTCOME_FAILED,
    OUTCOME_REVIEW,
    RentalTranzila,
    call_gateway,
    charge_due,
    issue_receipt_safely,
    mark_charged,
    outcome_of,
    retry_charge,
)
from apps.rental_billing.card import apply_card
from apps.rental_billing.errors import BillingError
from apps.rental_billing.models import TenantCharge, TenantStandingOrder
from apps.rental_billing.tests.factories import (
    CARD, CHARGES_URL, CRON_URL, DECLINE, ORDERS_URL, OK_TOKEN_CHARGE, BillingFixture, card_url,
)

Order = TenantStandingOrder
Charge = TenantCharge
SEP = date(2026, 9, 1)
SEP11 = date(2026, 9, 11)
OCT10 = date(2026, 10, 10)
CREDENTIALS = {
    'terminal': 'term-x', 'token_terminal': 'tok-x', 'supplier': 'sup-x',
    'public_key': 'pk-real-looking', 'secret_key': 'sk-real-looking',
}


def http_response(status_code, body=None):
    response = MagicMock(status_code=status_code)
    response.json.return_value = body
    return response


class AmbiguousAnswerTests(SimpleTestCase):
    """An answer that is not plainly a decline is review — and both readings agree."""

    def outcomes(self, body):
        app = RentalTranzila(**CREDENTIALS)
        with patch('apps.core.tranzila_service.requests.post', return_value=http_response(200, body)):
            through_json = outcome_of(call_gateway(
                lambda: app.charge_with_token(token='tok', amount=Decimal('1.00'), expire_month=12, expire_year=2030),
                app,
            ))
        plain = TranzilaService(**CREDENTIALS)
        with patch('apps.core.tranzila_service.requests.post', return_value=http_response(200, body)):
            without_json = outcome_of(plain.charge_with_token(
                token='tok', amount=Decimal('1.00'), expire_month=12, expire_year=2030,
            ))
        return through_json, without_json

    def test_a_success_with_no_transaction_id_is_review(self):
        for body in ({'error_code': 0}, {'error_code': 0, 'transaction_result': {'processor_response_code': ''}}):
            self.assertEqual(self.outcomes(body), (OUTCOME_REVIEW, OUTCOME_REVIEW), body)

    def test_an_approved_processor_code_with_no_error_code_is_review_both_ways(self):
        body = {'transaction_result': {'processor_response_code': '000', 'transaction_id': 'T9'}}
        self.assertEqual(self.outcomes(body), (OUTCOME_REVIEW, OUTCOME_REVIEW))

    def test_a_call_that_never_left_the_process_is_failed(self):
        service = RentalTranzila(**CREDENTIALS)
        result = call_gateway(lambda: service.charge_with_token(token='', amount=Decimal('1.00')), service)
        self.assertTrue(result['never_sent'])
        self.assertEqual(outcome_of(result), OUTCOME_FAILED)
        self.assertEqual(service.requests_made, 0)

        missing_expiry = RentalTranzila(**CREDENTIALS)
        result = call_gateway(
            lambda: missing_expiry.charge_with_token(token='tok', amount=Decimal('1.00')), missing_expiry,
        )
        self.assertEqual(outcome_of(result), OUTCOME_FAILED)

    def test_an_answer_that_did_leave_the_process_is_still_review(self):
        service = RentalTranzila(**CREDENTIALS)
        with patch('apps.core.tranzila_service.requests.post', return_value=http_response(504, None)):
            result = call_gateway(
                lambda: service.charge_with_token(
                    token='tok', amount=Decimal('1.00'), expire_month=12, expire_year=2030,
                ),
                service,
            )
        self.assertNotIn('never_sent', result)
        self.assertEqual(outcome_of(result), OUTCOME_REVIEW)
        self.assertEqual(service.requests_made, 1)


@override_settings(RENTAL_BILLING_ENABLED=True)
class BlockedTenancyTests(BillingFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.order_ = self.active_order(next_charge_date=OCT10)
        self.review = self.charge_row(self.order_, SEP, Charge.STATUS_REVIEW)

    def test_every_run_names_the_tenancy_it_cannot_touch(self):
        for day in (OCT10, date(2026, 11, 10), date(2026, 12, 10)):
            summary = charge_due(today=day)

        self.assertEqual(self.gateway_calls(), 0)
        self.assertEqual(summary['blocked'], [{
            'standing_order': str(self.order_.pk),
            'tenancy': str(self.tenancy.pk),
            'due': OCT10.isoformat(),
            'period': SEP.isoformat(),
            'charge': str(self.review.pk),
            'status': Charge.STATUS_REVIEW,
        }])
        self.assertIn('חסום', summary['errors'][0])
        self.assertIn(SEP.isoformat(), summary['errors'][0])

    def test_the_order_screen_shows_what_blocks_it_and_stops_when_it_is_decided(self):
        self.client.force_authenticate(self.manager)
        row = self.client.get(ORDERS_URL).data[0]
        self.assertEqual(row['blocked_by_charge']['period'], SEP.isoformat())
        self.assertEqual(row['blocked_by_charge']['status_label'], 'בבדיקה')

        mark_charged(self.review, transaction_id='T-found', user=self.manager)
        self.assertIsNone(self.client.get(ORDERS_URL).data[0]['blocked_by_charge'])
        summary = charge_due(today=OCT10)
        self.assertEqual((summary['charged'], summary['blocked']), (1, []))

    def test_a_reservation_that_never_heard_back_blocks_and_is_named(self):
        Charge.objects.filter(pk=self.review.pk).update(
            status=Charge.STATUS_RESERVED, reserved_at=timezone.now() - timedelta(hours=30),
        )
        summary = charge_due(today=OCT10)
        self.assertEqual(summary['stale_to_review'], 1)
        self.assertEqual(summary['blocked'][0]['period'], SEP.isoformat())
        self.assertEqual(self.gateway_calls(), 0)


@override_settings(RENTAL_BILLING_ENABLED=True, CRON_TOKEN='cron-secret')
class MissedMonthsAreKeptTests(BillingFixture, APITestCase):
    def test_the_run_is_recorded_with_the_months_it_left_behind(self):
        order = self.active_order(next_charge_date=date(2026, 7, 10))
        self.today.return_value = OCT10

        res = self.client.get(CRON_URL, HTTP_X_CRON_TOKEN='cron-secret')

        self.assertEqual(res.status_code, 200)
        # September only: July and August are before this order's own start.
        missed = [row['period'] for row in res.data['summary']['missed']]
        self.assertEqual(missed, ['2026-09-01'])
        kept = CronHeartbeat.objects.first().summary['rental_billing']
        self.assertEqual([row['period'] for row in kept['missed']], missed)
        self.assertEqual(kept['charged'], 1)

        # And the months stay readable on the order itself, run or no run.
        self.client.force_authenticate(self.manager)
        row = self.client.get(f'{ORDERS_URL}{order.pk}/').data
        self.assertEqual(row['months_never_charged'], ['2026-09-01'])
        self.assertEqual(row['next_charge_date'], '2026-11-10')

    def test_a_month_billed_is_never_listed_as_missed(self):
        order = self.active_order(next_charge_date=OCT10)
        self.today.return_value = OCT10
        charge_due(today=OCT10)
        self.client.force_authenticate(self.manager)
        row = self.client.get(f'{ORDERS_URL}{order.pk}/').data
        self.assertEqual(row['months_never_charged'], ['2026-09-01'])


@override_settings(RENTAL_BILLING_ENABLED=True)
class RetryGuardTests(BillingFixture, TestCase):
    def test_a_retry_the_same_day_as_a_charge_is_refused(self):
        today = timezone.localdate()
        self.today.return_value = today
        order = self.active_order(next_charge_date=today, billing_day=today.day)
        old = self.charge_row(
            order, date(2026, 8, 1), Charge.STATUS_FAILED, reserved_at=timezone.now() - timedelta(days=60),
        )
        summary = charge_due(today=today)
        self.assertEqual(summary['charged'], 1)

        with self.assertRaises(BillingError) as ctx:
            retry_charge(Charge.objects.get(pk=old.pk), user=self.manager)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn('היום', ctx.exception.message)
        self.assertEqual(self.gateway.charge_with_token.call_count, 1)
        self.assertEqual(Charge.objects.get(pk=old.pk).status, Charge.STATUS_FAILED)
        self.assertEqual(Charge.objects.filter(tenancy=self.tenancy, reserved_at__date=today).count(), 1)

    def test_a_retry_is_refused_when_the_order_ended_meanwhile(self):
        order = self.active_order(next_charge_date=OCT10)
        self.gateway.charge_with_token.return_value = dict(DECLINE)
        charge_due(today=OCT10)
        charge = Charge.objects.get()
        Order.objects.filter(pk=order.pk).update(status=Order.STATUS_ENDED)
        with self.assertRaises(BillingError):
            retry_charge(charge, user=self.manager)
        self.assertEqual(Charge.objects.get(pk=charge.pk).status, Charge.STATUS_FAILED)
        self.assertEqual(self.gateway.charge_with_token.call_count, 1)


@override_settings(RENTAL_BILLING_ENABLED=True)
class AmbiguousChargeIsNotRecordedAsPaidTests(BillingFixture, TestCase):
    def test_a_success_with_no_transaction_id_goes_to_review_and_gets_no_receipt(self):
        order = self.active_order(next_charge_date=OCT10)
        self.gateway.charge_with_token.return_value = {
            'success': True, 'transaction_id': '', 'confirmation_code': '', 'raw_response': {},
        }
        summary = charge_due(today=OCT10)
        self.assertEqual((summary['charged'], summary['review']), (0, 1))
        charge = Charge.objects.get()
        self.assertEqual(charge.status, Charge.STATUS_REVIEW)
        self.assertIsNone(charge.receipt_id)
        order.refresh_from_db()
        self.assertEqual((order.status, order.next_charge_date), (Order.STATUS_ACTIVE, OCT10))


@override_settings(RENTAL_BILLING_ENABLED=True)
class AnonymousCallsWriteNothingTests(BillingFixture, APITestCase):
    def test_an_unknown_token_does_not_touch_the_charges_table(self):
        order = self.active_order()
        stale = self.charge_row(
            order, SEP, Charge.STATUS_RESERVED, reserved_at=timezone.now() - timedelta(hours=2),
        )
        res = self.client.get('/api/v1/rental-billing/card/doesnotexist/')
        self.assertEqual(res.status_code, 404)
        stale.refresh_from_db()
        self.assertEqual(stale.status, Charge.STATUS_RESERVED)

        # A link that does exist still sweeps.
        self.client.get(card_url(self.link(order)))
        stale.refresh_from_db()
        self.assertEqual(stale.status, Charge.STATUS_REVIEW)


@override_settings(RENTAL_BILLING_ENABLED=True)
class VerifyNeverMovesTheScheduleBackTests(BillingFixture, TestCase):
    def test_a_verify_keeps_a_later_next_charge_date(self):
        order = self.active_order(
            next_charge_date=date(2027, 5, 10), status=Order.STATUS_FAILED, billing_day=10,
        )
        self.charge_row(order, SEP, Charge.STATUS_VOIDED)

        result = apply_card(self.link(order).token, CARD, today=SEP11)

        self.assertFalse(result['charged'])
        order.refresh_from_db()
        self.assertEqual(order.next_charge_date, date(2027, 5, 10))
        self.assertEqual(result['next_charge_date'], '2027-05-10')

    def test_a_verify_takes_a_month_charged_while_the_gateway_answered_into_account(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10), status=Order.STATUS_FAILED)
        self.charge_row(order, SEP, Charge.STATUS_VOIDED)
        charged_meanwhile = self.charge_row(order, date(2026, 10, 1), Charge.STATUS_CHARGED)

        def verify_then_charge_october(**kwargs):
            Charge.objects.filter(pk=charged_meanwhile.pk).update(charged_at=timezone.now())
            return dict(self.gateway.verify_card.return_value)

        self.gateway.verify_card.side_effect = verify_then_charge_october
        apply_card(self.link(order).token, CARD, today=SEP11)

        order.refresh_from_db()
        self.assertEqual(order.next_charge_date, date(2026, 11, 10))


@override_settings(RENTAL_BILLING_ENABLED=True)
class LateReceiptAcrossTheYearTests(BillingFixture, TestCase):
    def test_a_december_charge_receipted_now_is_numbered_and_dated_now(self):
        order = self.active_order()
        charged_at = timezone.make_aware(datetime(2026, 12, 31, 22, 0))
        charge = self.charge_row(
            order, date(2026, 12, 1), Charge.STATUS_CHARGED, charged_at=charged_at, transaction_id='T-dec',
        )
        self.assertTrue(issue_receipt_safely(charge.pk))

        today = timezone.localdate()
        doc = Charge.objects.get(pk=charge.pk).receipt
        self.assertTrue(doc.document_number.startswith(f'RT-{today.year}-'))
        self.assertEqual(doc.document_date, today)
        self.assertIn('31/12/2026', doc.customer_notes)
        self.assertIn('הופק באיחור', doc.customer_notes)
