"""Shared setup for the rental billing tests.

Tranzila is always a mock here. The service class the app reaches it through is
replaced by a MagicMock, and the real client's request method is made to fail
the test, so nothing can leave the machine even by a path the mock missed.
"""
from datetime import date
from unittest.mock import MagicMock, patch

from django.core.cache import cache

from apps.core.models import Business, UserProfile
from apps.rental_billing.links import new_token
from apps.rental_billing.models import TenantCardLink, TenantCharge, TenantStandingOrder
from apps.rental_billing.orders import open_standing_order
from apps.rentals.models import RentalContract
from apps.rentals.tests.factories import make_branch, make_customer, make_tenancy, make_user  # noqa: F401

CARD = {
    'card_number': '4580000000000000', 'expiry_month': 12, 'expiry_year': 2030,
    'cvv': '123', 'card_holder_id': '039876545',
}
OK_TOKEN_CHARGE = {
    'success': True, 'transaction_id': 'T100', 'confirmation_code': 'C100', 'response_code': '000',
    'raw_response': {},
}
OK_CARD_CHARGE = {**OK_TOKEN_CHARGE, 'token': 'tok_new', 'transaction_id': 'T200', 'confirmation_code': 'C200'}
OK_VERIFY = {
    'success': True, 'token': 'tok_verified', 'transaction_id': 'V1', 'confirmation_code': '',
    'response_code': '000', 'amount': 0.0, 'raw_response': {},
}
DECLINE = {'success': False, 'error': 'Card declined', 'response_code': '033', 'message': 'Charge failed: Card declined'}
TIMEOUT = {'success': False, 'error': 'Read timed out', 'response_code': '999', 'uncertain': True}

# make_tenancy agrees on 1,234.56 before VAT: 1,456.78 with 18% VAT.
NET_AGOROT, VAT_AGOROT, TOTAL_AGOROT = 123456, 22222, 145678

ORDERS_URL = '/api/v1/rental-billing/standing-orders/'
CHARGES_URL = '/api/v1/rental-billing/charges/'
CRON_URL = '/api/v1/rental-billing/cron/charge/'
STATUS_URL = '/api/v1/rental-billing/status/'


def card_url(link) -> str:
    return f'/api/v1/rental-billing/card/{link.token}/'


def sign_contract(tenancy) -> RentalContract:
    """A signed contract for the tenancy. Issued as a draft, then signed the one way the model allows."""
    from apps.rentals.tests.factories import sign_directly

    draft = RentalContract.objects.create(tenancy=tenancy, version=1, terms={'v': 1}, pdf=b'%PDF-1.4')
    return sign_directly(draft)


def mocked_gateway() -> MagicMock:
    gateway = MagicMock(name='tranzila')
    # No JSON kept: outcomes are read from the result, as TranzilaService shapes it.
    gateway.last_response = None
    gateway.credential_error.return_value = None
    gateway.charge_with_token.return_value = dict(OK_TOKEN_CHARGE)
    gateway.charge_with_card.return_value = dict(OK_CARD_CHARGE)
    gateway.verify_card.return_value = dict(OK_VERIFY)
    return gateway


def patch_tranzila(testcase, gateway) -> MagicMock:
    """Route the app's gateway() to `gateway`, and make any real Tranzila request fail the test."""
    service = patch('apps.rental_billing.billing.RentalTranzila')
    tranzila_class = service.start()
    tranzila_class.return_value = gateway
    testcase.addCleanup(service.stop)
    real = patch(
        'apps.core.tranzila_service.TranzilaService._make_api_request',
        side_effect=AssertionError('a real Tranzila request was made'),
    )
    real.start()
    testcase.addCleanup(real.stop)
    # No real e-mail either: a receipt mailed through Resend fails the test. A test
    # that sends through the locmem backend patches this again on its own.
    resend = patch(
        'apps.rental_billing.receipt_email.send_resend_email',
        side_effect=AssertionError('a real e-mail was sent'),
    )
    resend.start()
    testcase.addCleanup(resend.stop)
    return tranzila_class


class BillingFixture:
    """A branch, a tenancy with its agreed amount, the rentals business, and a mocked gateway."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.branch = make_branch('פלורנטין')
        self.tenancy = make_tenancy(self.branch)
        self.business, _ = Business.objects.get_or_create(name='סוחרים')
        self.manager = make_user('manager-rental-billing@test', UserProfile.ROLE_MANAGER)
        self.gateway = mocked_gateway()
        self.tranzila_class = patch_tranzila(self, self.gateway)
        # "Today" for everything that does not take it as an argument: a fixed
        # day, so the schedule the tests read does not move with the calendar.
        today = patch('apps.rental_billing.billing.today_local', return_value=date(2026, 9, 11))
        self.today = today.start()
        self.addCleanup(today.stop)

    def gateway_calls(self) -> int:
        return (
            self.gateway.charge_with_token.call_count
            + self.gateway.charge_with_card.call_count
            + self.gateway.verify_card.call_count
        )

    def order(self, tenancy=None, **fields) -> TenantStandingOrder:
        order = open_standing_order(tenancy or self.tenancy, user=self.manager)
        if fields:
            TenantStandingOrder.objects.filter(pk=order.pk).update(**fields)
            order.refresh_from_db()
        return order

    def active_order(self, next_charge_date=date(2026, 10, 10), tenancy=None, **fields) -> TenantStandingOrder:
        values = {
            'status': TenantStandingOrder.STATUS_ACTIVE, 'tranzila_token': 'tok_saved', 'card_expire_month': 12,
            'card_expire_year': 2030, 'card_last4': '4242', 'next_charge_date': next_charge_date, **fields,
        }
        return self.order(tenancy=tenancy, **values)

    def charge_row(self, order, period, status, **fields) -> TenantCharge:
        values = {
            'amount_before_vat': NET_AGOROT, 'vat_amount': VAT_AGOROT, 'total': TOTAL_AGOROT,
            'business': self.business, 'trigger': TenantCharge.TRIGGER_CRON, 'attempts': 1, **fields,
        }
        return TenantCharge.objects.create(
            standing_order=order, tenancy_id=order.tenancy_id, period=period, status=status, **values,
        )

    def link(self, order, **fields) -> TenantCardLink:
        link = TenantCardLink.objects.create(standing_order=order, token=new_token())
        if fields:
            TenantCardLink.objects.filter(pk=link.pk).update(**fields)
            link.refresh_from_db()
        return link
