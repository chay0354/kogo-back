"""Regression tests for the money review of phase 4.

Each of the reviewer's probes (scratchpad/probes/rb_review_probes.py) is here the
other way round: it failed before the fix and passes after it. Around them, the
tests for the rest of the review — the rental terminal set, the once-a-day guard,
the batch, the stale sweep, the late receipt, the order re-checked on the card page.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.tranzila_service import TranzilaService
from apps.rental_billing import card as card_module
from apps.rental_billing.billing import (
    OUTCOME_CHARGED, OUTCOME_FAILED, OUTCOME_REVIEW, RentalTranzila, call_gateway, charge_due,
    issue_receipt_safely, outcome_of, rental_credentials, retry_charge, terminal_report, void_charge, gateway,
)
from apps.rental_billing.card import CardEntryError, apply_card, preview_payload
from apps.rental_billing.models import TenantCardLink, TenantCharge, TenantStandingOrder
from apps.rental_billing.orders import end_order, open_standing_order
from apps.rental_billing.schedule import add_months, first_of_month
from apps.rental_billing.tests.factories import (
    CARD, CHARGES_URL, DECLINE, OK_CARD_CHARGE, OK_TOKEN_CHARGE, STATUS_URL, BillingFixture, card_url,
    make_customer, make_tenancy,
)

Order = TenantStandingOrder
Charge = TenantCharge
Link = TenantCardLink
SEP = date(2026, 9, 1)
SEP11 = date(2026, 9, 11)
CREDENTIALS = {
    'terminal': 'term-x', 'token_terminal': 'tok-x', 'supplier': 'sup-x',
    'public_key': 'pk-real-looking', 'secret_key': 'sk-real-looking',
}


def http_response(status_code, body=None, *, broken=False):
    response = MagicMock(status_code=status_code)
    if broken:
        response.json.side_effect = ValueError('no json')
    else:
        response.json.return_value = body
    return response


def plain_504_result() -> dict:
    """What charge_with_card returns when Tranzila (or its proxy) answers HTTP 504 with an HTML page."""
    svc = TranzilaService(**CREDENTIALS)
    raw = svc._build_error_response('HTTP 504', '999', 'API request failed')
    return svc._parse_credit_card_create_response(raw, amount=Decimal('1.00'), last4='0000')


class GatewayOutcomeTests(SimpleTestCase):
    """Only an explicit decline in Tranzila's JSON is 'failed'. Everything else may have charged the card."""

    def through_the_app(self, **post):
        """RentalTranzila and call_gateway: the outcome read from the JSON Tranzila answered with."""
        svc = RentalTranzila(**CREDENTIALS)
        with patch('apps.core.tranzila_service.requests.post', **post):
            result = call_gateway(
                lambda: svc.charge_with_token(token='tok', amount=Decimal('1.00'), expire_month=12, expire_year=2030),
                svc,
            )
        return outcome_of(result)

    def through_the_plain_service(self, **post):
        """The probe's path: TranzilaService alone, no JSON kept — read by the result's shape."""
        svc = TranzilaService(**CREDENTIALS)
        with patch('apps.core.tranzila_service.requests.post', **post):
            result = svc.charge_with_token(token='tok', amount=Decimal('1.00'), expire_month=12, expire_year=2030)
        return outcome_of(result)

    def assert_outcome(self, expected, **post):
        self.assertEqual(self.through_the_app(**post), expected)
        self.assertEqual(self.through_the_plain_service(**post), expected)

    def test_an_http_504_with_an_html_page_is_review(self):
        self.assert_outcome(OUTCOME_REVIEW, return_value=http_response(504, broken=True))

    def test_an_http_502_with_json_that_is_not_tranzilas_answer_is_review(self):
        self.assert_outcome(OUTCOME_REVIEW, return_value=http_response(502, {'message': 'Bad Gateway'}))

    def test_a_200_with_a_broken_body_is_review(self):
        self.assert_outcome(OUTCOME_REVIEW, return_value=http_response(200, broken=True))

    def test_a_connection_dropped_mid_response_is_review(self):
        self.assert_outcome(
            OUTCOME_REVIEW,
            side_effect=requests.exceptions.ChunkedEncodingError('Connection broken: IncompleteRead(0 bytes read)'),
        )

    def test_a_read_timeout_is_review(self):
        self.assert_outcome(OUTCOME_REVIEW, side_effect=requests.exceptions.ReadTimeout('Read timed out'))

    def test_a_200_whose_json_says_nothing_is_review(self):
        self.assert_outcome(OUTCOME_REVIEW, return_value=http_response(200, {'status': 'ok?'}))

    def test_a_clean_decline_from_the_processor_is_failed(self):
        body = {'error_code': 0, 'message': 'declined', 'transaction_result': {'processor_response_code': '033'}}
        self.assert_outcome(OUTCOME_FAILED, return_value=http_response(200, body))

    def test_an_application_error_code_is_failed_even_on_an_http_error(self):
        body = {'error_code': 20004, 'message': 'Invalid card'}
        self.assert_outcome(OUTCOME_FAILED, return_value=http_response(200, body))
        self.assert_outcome(OUTCOME_FAILED, return_value=http_response(400, body))

    def test_a_clean_success_is_charged(self):
        body = {'error_code': 0, 'transaction_result': {
            'processor_response_code': '000', 'transaction_id': 'T1', 'ConfirmationCode': 'C1',
        }}
        self.assert_outcome(OUTCOME_CHARGED, return_value=http_response(200, body))

    # ------------------------------------------------------------ terminals

    @override_settings(
        RENTAL_BILLING_ENABLED=True, TRANZILA_PROD_TERMINAL='prod-t', TRANZILA_PROD_TOKEN_TERMINAL='prod-tok',
        TRANZILA_PROD_SUPPLIER='prod-s', TRANZILA_PROD_PUBLIC_KEY='prod-pk', TRANZILA_PROD_SECRET_KEY='prod-sk',
        RENTAL_TRANZILA_TERMINAL='', RENTAL_TRANZILA_TOKEN_TERMINAL='', RENTAL_TRANZILA_SUPPLIER='',
        RENTAL_TRANZILA_PUBLIC_KEY='', RENTAL_TRANZILA_SECRET_KEY='',
    )
    def test_the_rental_terminals_fall_back_to_the_production_ones(self):
        svc = gateway()
        self.assertIsInstance(svc, RentalTranzila)
        self.assertEqual(
            (svc.terminal, svc.token_terminal, svc.supplier, svc.public_key, svc.secret_key),
            ('prod-t', 'prod-tok', 'prod-s', 'prod-pk', 'prod-sk'),
        )
        self.assertEqual(terminal_report(), {
            'terminal_set': 'production', 'terminal': 'prod-t', 'token_terminal': 'prod-tok', 'overridden': [],
        })

    @override_settings(
        RENTAL_BILLING_ENABLED=True, TRANZILA_PROD_TERMINAL='prod-t', TRANZILA_PROD_TOKEN_TERMINAL='prod-tok',
        TRANZILA_PROD_SUPPLIER='prod-s', TRANZILA_PROD_PUBLIC_KEY='prod-pk', TRANZILA_PROD_SECRET_KEY='prod-sk',
        RENTAL_TRANZILA_TERMINAL='test-t', RENTAL_TRANZILA_TOKEN_TERMINAL='test-tok', RENTAL_TRANZILA_SUPPLIER='',
        RENTAL_TRANZILA_PUBLIC_KEY='test-pk', RENTAL_TRANZILA_SECRET_KEY='test-sk',
    )
    def test_the_rental_terminals_override_setting_by_setting(self):
        svc = gateway()
        self.assertEqual(
            (svc.terminal, svc.token_terminal, svc.supplier, svc.public_key, svc.secret_key),
            ('test-t', 'test-tok', 'prod-s', 'test-pk', 'test-sk'),
        )
        report = terminal_report()
        self.assertEqual(report['terminal_set'], 'mixed')
        self.assertEqual((report['terminal'], report['token_terminal']), ('test-t', 'test-tok'))
        self.assertNotIn('RENTAL_TRANZILA_SUPPLIER', report['overridden'])
        self.assertNotIn('test-sk', repr(report))
        self.assertNotIn('test-pk', repr(report))
        # The courses' client is not moved.
        production = TranzilaService.production()
        self.assertEqual((production.terminal, production.token_terminal), ('prod-t', 'prod-tok'))
        values, overridden = rental_credentials()
        self.assertEqual(len(overridden), 4)
        self.assertEqual(values['supplier'], 'prod-s')


@override_settings(RENTAL_BILLING_ENABLED=True)
class MoneyReviewTests(BillingFixture, TestCase):
    # ------------------------------------------------- 1. one month, one tenancy

    def test_a_month_is_charged_once_on_a_tenancy_across_two_orders(self):
        first = self.order()
        apply_card(self.link(first).token, CARD, today=SEP11)
        end_order(Order.objects.get(pk=first.pk))

        second = open_standing_order(self.tenancy, user=self.manager)
        self.assertEqual(second.start_date, date(2026, 10, 1))
        result = apply_card(self.link(second).token, CARD, today=SEP11)

        self.assertEqual((result['charged'], result['next_charge_date']), (False, '2026-10-10'))
        self.assertEqual(Charge.objects.filter(tenancy=self.tenancy, period=SEP).count(), 1)
        self.assertEqual(self.gateway.charge_with_card.call_count, 1)
        self.gateway.verify_card.assert_called_once()

    def test_an_earlier_start_typed_by_the_office_still_never_charges_the_month_again(self):
        first = self.order()
        apply_card(self.link(first).token, CARD, today=SEP11)
        end_order(Order.objects.get(pk=first.pk))
        second = open_standing_order(self.tenancy, user=self.manager, start_date=SEP)
        link = self.link(second)
        preview = preview_payload(link, today=SEP11)
        self.assertFalse(preview['charge_now'])
        result = apply_card(link.token, CARD, today=SEP11)
        self.assertFalse(result['charged'])
        self.assertEqual(self.gateway.charge_with_card.call_count, 1)
        self.assertEqual(Charge.objects.filter(tenancy=self.tenancy, period=SEP).count(), 1)

    def test_the_cron_never_charges_a_month_another_order_on_the_tenancy_charged(self):
        first = self.active_order(next_charge_date=date(2026, 10, 10))
        charge_due(today=date(2026, 10, 10))
        end_order(first)
        second = self.active_order(next_charge_date=date(2026, 10, 10))
        charge_due(today=date(2026, 10, 11))
        self.assertEqual(self.gateway.charge_with_token.call_count, 1)
        second.refresh_from_db()
        self.assertEqual(second.next_charge_date, date(2026, 11, 10))

    def test_the_database_holds_one_row_per_tenancy_and_month(self):
        first = self.order()
        self.charge_row(first, SEP, Charge.STATUS_CHARGED, charged_at=timezone.now())
        end_order(first)
        second = self.order()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.charge_row(second, SEP, Charge.STATUS_RESERVED)

    def test_a_new_order_starts_at_the_first_open_month_never_in_the_past(self):
        old = make_tenancy(self.branch, tenant=make_customer('ותיק', 'שוכר'), start_date=date(2026, 7, 1))
        self.assertEqual(open_standing_order(old, today=SEP11).start_date, SEP)
        future = make_tenancy(self.branch, tenant=make_customer('עתידי', 'שוכר'), start_date=date(2026, 10, 15))
        self.assertEqual(open_standing_order(future, today=SEP11).start_date, date(2026, 10, 15))

    # ---------------------------------------------- 2. an unclear answer is review

    def test_a_504_on_the_card_page_is_review_and_the_month_is_never_sent_again(self):
        order = self.order()
        link = self.link(order)
        self.gateway.charge_with_card.return_value = plain_504_result()

        with self.assertRaises(CardEntryError) as ctx:
            apply_card(link.token, CARD, today=SEP11)

        self.assertTrue(ctx.exception.processing)
        link.refresh_from_db()
        self.assertEqual(link.status, Link.STATUS_REVIEW)
        self.assertEqual(Charge.objects.get().status, Charge.STATUS_REVIEW)
        self.gateway.charge_with_card.return_value = dict(OK_CARD_CHARGE)
        with self.assertRaises(CardEntryError):
            apply_card(link.token, CARD, today=SEP11)
        # A new link cannot send the month either while it is in review.
        fresh_link = self.link(order)
        with self.assertRaises(CardEntryError) as again:
            apply_card(fresh_link.token, CARD, today=SEP11)
        self.assertTrue(again.exception.processing)
        self.assertEqual(self.gateway.charge_with_card.call_count, 1)

    def test_a_504_on_the_monthly_run_is_review_and_the_order_is_not_failed(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        self.gateway.charge_with_token.return_value = plain_504_result()
        summary = charge_due(today=date(2026, 10, 10))
        self.assertEqual(summary['review'], 1)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_ACTIVE)
        self.assertFalse(Link.objects.exists())

    def test_an_office_retry_that_gets_a_504_is_review_not_another_failure(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        self.gateway.charge_with_token.return_value = dict(DECLINE)
        charge_due(today=date(2026, 10, 10))
        self.gateway.charge_with_token.return_value = plain_504_result()
        outcome, charge = retry_charge(Charge.objects.get(), user=self.manager)
        self.assertEqual((outcome, charge.status), (OUTCOME_REVIEW, Charge.STATUS_REVIEW))
        with self.assertRaises(Exception):
            retry_charge(charge, user=self.manager)
        self.assertEqual(self.gateway.charge_with_token.call_count, 2)

    # ------------------------------------------------------ 3. no back months

    def test_after_a_void_a_late_card_charges_the_current_month_and_no_back_month(self):
        order = self.active_order(next_charge_date=date(2026, 9, 10))
        self.gateway.charge_with_token.return_value = dict(DECLINE)
        charge_due(today=date(2026, 9, 10))
        void_charge(Charge.objects.get(standing_order=order), reason='שולם במזומן', user=self.manager)
        link = Link.objects.get(standing_order=order, status=Link.STATUS_PENDING)
        dec15 = date(2026, 12, 15)

        preview = preview_payload(link, today=dec15)
        self.assertEqual(
            (preview['charge_now'], preview['charge_period'], preview['next_charge_date']),
            (True, '2026-12-01', '2027-01-10'),
        )
        result = apply_card(link.token, CARD, today=dec15)
        self.assertEqual((result['charged'], result['next_charge_date']), (True, '2027-01-10'))
        self.gateway.charge_with_token.return_value = dict(OK_TOKEN_CHARGE)
        summary = charge_due(today=dec15)
        self.assertEqual(summary['charged'], 0)
        self.assertEqual(
            sorted(Charge.objects.values_list('period', flat=True)), [date(2026, 9, 1), date(2026, 12, 1)],
        )

    def test_a_late_card_before_the_billing_day_charges_nothing_and_shows_a_real_date(self):
        order = self.active_order(next_charge_date=date(2026, 9, 10), status=Order.STATUS_FAILED)
        link = self.link(order)
        preview = preview_payload(link, today=date(2026, 12, 5))
        self.assertEqual((preview['charge_now'], preview['next_charge_date']), (False, '2026-12-10'))
        result = apply_card(link.token, CARD, today=date(2026, 12, 5))
        self.assertEqual((result['charged'], result['next_charge_date']), (False, '2026-12-10'))

    def test_a_void_resume_and_retry_never_leave_the_next_charge_in_a_past_month(self):
        order = self.active_order(next_charge_date=date(2026, 9, 10))
        review = self.charge_row(order, SEP, Charge.STATUS_REVIEW)
        self.today.return_value = date(2026, 12, 15)
        void_charge(review, reason='לא חויב', user=self.manager)
        order.refresh_from_db()
        self.assertEqual(order.next_charge_date, date(2026, 12, 10))

    # --------------------------------------------------- 4. once a day, per tenancy

    def test_a_retried_month_and_the_next_one_are_never_charged_the_same_day(self):
        today = timezone.localdate()
        self.today.return_value = today
        last_month = add_months(first_of_month(today), -1)
        order = self.active_order(next_charge_date=last_month, billing_day=1)
        self.gateway.charge_with_token.return_value = dict(DECLINE)
        charge_due(today=last_month)
        charge = Charge.objects.get(standing_order=order)
        month_ago = timezone.now() - timedelta(days=30)
        Charge.objects.filter(pk=charge.pk).update(created_at=month_ago, reserved_at=month_ago)
        self.gateway.charge_with_token.return_value = dict(OK_TOKEN_CHARGE)

        retry_charge(Charge.objects.get(pk=charge.pk), user=self.manager)
        summary = charge_due(today=today)

        self.assertEqual(summary['charged'], 0)
        self.assertEqual(self.gateway.charge_with_token.call_count, 2)

    # -------------------------------------------------------- 8. the order re-checked

    def test_the_card_page_rechecks_the_order_when_it_reserves(self):
        order = self.order()
        link = self.link(order)
        original = card_module.plan_for

        def plan_then_end(o, today):
            plan = original(o, today)
            Order.objects.filter(pk=o.pk).update(status=Order.STATUS_ENDED)
            return plan

        with patch.object(card_module, 'plan_for', side_effect=plan_then_end):
            with self.assertRaises(CardEntryError) as ctx:
                apply_card(link.token, CARD, today=SEP11)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.gateway_calls(), 0)
        self.assertFalse(Charge.objects.exists())
        link.refresh_from_db()
        self.assertEqual(link.status, Link.STATUS_PENDING)

    # ----------------------------------------------------------- 9. the batch

    def test_an_order_waiting_on_review_does_not_use_up_the_batch(self):
        stuck = self.active_order(next_charge_date=date(2026, 10, 10))
        self.charge_row(stuck, date(2026, 9, 1), Charge.STATUS_REVIEW)
        other = make_tenancy(self.branch, tenant=make_customer('שני', 'שוכר'))
        due = self.active_order(next_charge_date=date(2026, 10, 10), tenancy=other)
        summary = charge_due(today=date(2026, 10, 10), limit=1)
        self.assertEqual((summary['checked'], summary['charged']), (1, 1))
        self.assertTrue(Charge.objects.filter(standing_order=due, status=Charge.STATUS_CHARGED).exists())
        self.assertFalse(Charge.objects.filter(standing_order=stuck, period=date(2026, 10, 1)).exists())

    # ------------------------------------------------------------ 7. late receipt

    def test_a_receipt_issued_after_the_charge_day_is_dated_today_and_marked_late(self):
        order = self.active_order()
        charged_at = timezone.now() - timedelta(days=3)
        early = self.charge_row(order, date(2026, 10, 1), Charge.STATUS_CHARGED, charged_at=charged_at,
                                transaction_id='T1', confirmation_code='C1')
        on_time = self.charge_row(order, date(2026, 11, 1), Charge.STATUS_CHARGED, charged_at=timezone.now())
        self.assertTrue(issue_receipt_safely(on_time.pk))
        self.assertTrue(issue_receipt_safely(early.pk))

        today = timezone.localdate()
        first = Charge.objects.get(pk=on_time.pk).receipt
        late = Charge.objects.get(pk=early.pk).receipt
        self.assertEqual((first.document_date, late.document_date), (today, today))
        self.assertLess(first.document_number, late.document_number)
        self.assertTrue(late.document_number.startswith(f'RT-{today.year}-'))
        charged_on = timezone.localdate(charged_at)
        self.assertIn('הופק באיחור', late.customer_notes)
        self.assertIn(f'{charged_on:%d/%m/%Y}', late.customer_notes)
        self.assertIn(f'{today:%d/%m/%Y}', late.customer_notes)
        self.assertEqual(first.customer_notes, '')


@override_settings(RENTAL_BILLING_ENABLED=True)
class StaleSweepAndStatusTests(BillingFixture, APITestCase):
    def stale(self, order, period):
        return self.charge_row(order, period, Charge.STATUS_RESERVED, reserved_at=timezone.now() - timedelta(minutes=20))

    def test_the_office_list_shows_a_reservation_that_never_heard_back_as_review(self):
        order = self.active_order()
        charge = self.stale(order, date(2026, 9, 1))
        self.client.force_authenticate(self.manager)
        rows = self.client.get(CHARGES_URL).data
        self.assertEqual(rows[0]['status'], 'review')
        charge.refresh_from_db()
        self.assertEqual(charge.status, Charge.STATUS_REVIEW)

    def test_the_card_page_turns_it_to_review_when_it_opens(self):
        order = self.order()
        charge = self.stale(order, SEP)
        page = self.client.get(card_url(self.link(order)))
        self.assertEqual(page.status_code, 200)
        self.assertIn('error', page.data)
        charge.refresh_from_db()
        self.assertEqual(charge.status, Charge.STATUS_REVIEW)

    @override_settings(RENTAL_TRANZILA_TERMINAL='test-t', RENTAL_TRANZILA_SECRET_KEY='never-shown')
    def test_status_names_the_terminal_set_and_never_a_key(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(STATUS_URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['tranzila']['terminal'], 'test-t')
        self.assertEqual(res.data['tranzila']['terminal_set'], 'mixed')
        self.assertIn('RENTAL_TRANZILA_SECRET_KEY', res.data['tranzila']['overridden'])
        self.assertNotIn('never-shown', repr(res.data))
