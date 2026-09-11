"""The month's reservation is committed before Tranzila is called — seen from another
database connection while the gateway call is running, with no transaction open around it.

A TransactionTestCase: inside a TestCase every block is a savepoint of the test's own
transaction, and nothing is ever visible to another connection.
"""
import threading
from datetime import date

from django.db import connection, connections
from django.test import TransactionTestCase, override_settings

from apps.core.models import Business
from apps.rental_billing.billing import charge_due, retry_charge
from apps.rental_billing.card import apply_card
from apps.rental_billing.models import TenantCardLink, TenantCharge, TenantStandingOrder
from apps.rental_billing.tests.factories import (
    CARD, DECLINE, OK_CARD_CHARGE, OK_TOKEN_CHARGE, BillingFixture,
)


def statuses_seen_from_another_connection(order_id) -> list:
    seen = {}

    def look():
        try:
            seen['statuses'] = list(
                TenantCharge.objects.filter(standing_order_id=order_id).values_list('status', flat=True)
            )
        finally:
            connections.close_all()

    thread = threading.Thread(target=look)
    thread.start()
    thread.join()
    return seen['statuses']


@override_settings(RENTAL_BILLING_ENABLED=True)
class ReservationIsCommittedFirstTests(BillingFixture, TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        Business.objects.get_or_create(name='סוחרים')
        super().setUp()
        self.seen = []

    def watch(self, answer):
        def call(**kwargs):
            order_id = TenantStandingOrder.objects.values_list('pk', flat=True).first()
            self.seen.append({
                'in_transaction': connection.in_atomic_block,
                'statuses': statuses_seen_from_another_connection(order_id),
            })
            return dict(answer)
        return call

    def test_the_cron(self):
        self.active_order(next_charge_date=date(2026, 10, 10))
        self.gateway.charge_with_token.side_effect = self.watch(OK_TOKEN_CHARGE)
        charge_due(today=date(2026, 10, 10))
        self.assertEqual(self.seen, [{'in_transaction': False, 'statuses': ['reserved']}])
        self.assertEqual(TenantCharge.objects.get().status, TenantCharge.STATUS_CHARGED)

    def test_the_card_page(self):
        order = self.order()
        link = self.link(order)
        self.gateway.charge_with_card.side_effect = self.watch(OK_CARD_CHARGE)
        apply_card(link.token, CARD, today=date(2026, 9, 11))
        self.assertEqual(self.seen, [{'in_transaction': False, 'statuses': ['reserved']}])
        self.assertEqual(TenantCardLink.objects.get().status, TenantCardLink.STATUS_USED)

    def test_the_offices_retry(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        self.gateway.charge_with_token.return_value = dict(DECLINE)
        charge_due(today=date(2026, 10, 10))
        self.gateway.charge_with_token.side_effect = self.watch(OK_TOKEN_CHARGE)
        retry_charge(TenantCharge.objects.get(), user=self.manager)
        self.assertEqual(self.seen, [{'in_transaction': False, 'statuses': ['reserved']}])
        order.refresh_from_db()
        self.assertEqual(order.status, TenantStandingOrder.STATUS_ACTIVE)
