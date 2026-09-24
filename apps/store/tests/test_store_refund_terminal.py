"""
A store refund goes back to the terminal that took the charge, with that
terminal's own keys and the card that paid.

Before 24.9.2026 every store refund went to the production token terminal with
the child's standing-order card: a website or walk-in purchase had no card to
send, and a child who paid with another card was credited to the standing
order's. The hosted page now runs on cogolive, whose keys are refused on the
michal terminals, so the terminal is kept on the invoice.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.core.payment_service import PaymentService
from apps.core.tranzila_service import TranzilaService
from apps.customers.models import Child, Family, RecurringPayment
from apps.store.models import StoreInvoice

TERMINALS = dict(
    TRANZILA_TERMINAL='cogolive',
    TRANZILA_TOKEN_TERMINAL='cogolivetok',
    TRANZILA_PUBLIC_KEY='cogolive_pk',
    TRANZILA_SECRET_KEY='cogolive_sk',
    TRANZILA_PROD_TERMINAL='fxpmichalweb',
    TRANZILA_PROD_TOKEN_TERMINAL='fxpmichalwebtok',
    TRANZILA_PROD_SUPPLIER='fxpmichalweb',
    TRANZILA_PROD_PUBLIC_KEY='michal_pk',
    TRANZILA_PROD_SECRET_KEY='michal_sk',
)
REFUNDED = {'success': True, 'transaction_id': '2', 'confirmation_code': '0000999', 'response_code': '000'}
# A /v1/transactions row as cogolive returns it (23.9.2026), card fields filled.
PAID_ROW = {
    'index': '1', 'amount': '4900', 'processor_response_code': '000', 'tranmode': 'A',
    'expiration_month': '07', 'expiration_year': '29', 'credit_card_token': 'card-that-paid-1234',
}


@override_settings(**TERMINALS)
@patch('apps.core.credit_note_email.send_credit_note_email')
class StoreRefundTerminalTest(TestCase):
    def setUp(self):
        family = Family.objects.create(name='כהן', phone='0501111111')
        self.child = Child.objects.create(
            family=family, first_name='נועה', last_name='כהן',
            birth_date=date(2016, 1, 1), gender='female', status='active',
        )
        RecurringPayment.objects.create(
            child=self.child, tranzila_token='the-standing-order-card', status='active', amount=Decimal('235'),
            start_date=date(2026, 9, 1), next_billing_date=date(2026, 10, 1),
            card_expire_month=12, card_expire_year=2030,
        )

    def invoice(self, **fields):
        defaults = dict(
            total_amount=Decimal('49.00'), payment_method='credit_card', payment_status='completed',
            tranzila_transaction_id='1', tranzila_confirmation_code='0000123',
        )
        defaults.update(fields)
        return StoreInvoice.objects.create(**defaults)

    def refund(self, invoice, *, report=PAID_ROW):
        """Refund with Tranzila faked; returns (result, refund call, report lookups)."""
        lookups = []

        def find(service, index):
            lookups.append((service.terminal, service.public_key, str(index)))
            return {'success': True, 'transaction': report}

        with patch.object(TranzilaService, 'refund_transaction', autospec=True, return_value=REFUNDED) as refund, \
                patch.object(TranzilaService, 'find_transaction', autospec=True, side_effect=find):
            result = PaymentService().refund_store_invoice(str(invoice.id), reason='החזרת מוצר')
        return result, refund, lookups

    def test_a_hosted_page_purchase_is_refunded_on_cogolive_to_the_card_that_paid(self, _email):
        invoice = self.invoice(tranzila_terminal='cogolive', child=self.child)

        result, refund, lookups = self.refund(invoice)

        self.assertTrue(result['success'], result)
        service = refund.call_args.args[0]
        kwargs = refund.call_args.kwargs
        self.assertEqual(kwargs['terminal_name'], 'cogolive')
        self.assertEqual(service.public_key, 'cogolive_pk')
        self.assertEqual(lookups, [('cogolive', 'cogolive_pk', '1')])
        # The card on the report, not the child's standing-order card.
        self.assertEqual(kwargs['token'], 'card-that-paid-1234')
        self.assertEqual((kwargs['card_expire_month'], kwargs['card_expire_year']), ('07', '29'))

    def test_a_walk_in_typed_card_is_refunded_on_the_card_terminal(self, _email):
        invoice = self.invoice(tranzila_terminal='fxpmichalweb')

        result, refund, lookups = self.refund(invoice)

        self.assertTrue(result['success'], result)
        self.assertEqual(refund.call_args.kwargs['terminal_name'], 'fxpmichalweb')
        self.assertEqual(refund.call_args.args[0].public_key, 'michal_pk')
        self.assertEqual(lookups, [('fxpmichalweb', 'michal_pk', '1')])

    def test_a_saved_card_purchase_is_refunded_to_the_standing_order_card(self, _email):
        invoice = self.invoice(tranzila_terminal='fxpmichalwebtok', child=self.child, charged_with_token=True)

        result, refund, lookups = self.refund(invoice)

        self.assertTrue(result['success'], result)
        kwargs = refund.call_args.kwargs
        self.assertEqual(kwargs['terminal_name'], 'fxpmichalwebtok')
        self.assertEqual(refund.call_args.args[0].public_key, 'michal_pk')
        self.assertEqual(kwargs['token'], 'the-standing-order-card')
        self.assertEqual(lookups, [])

    def test_an_invoice_from_before_keeps_the_old_route(self, _email):
        invoice = self.invoice(child=self.child)  # no terminal recorded

        result, refund, lookups = self.refund(invoice)

        self.assertTrue(result['success'], result)
        service = refund.call_args.args[0]
        kwargs = refund.call_args.kwargs
        self.assertIsNone(kwargs['terminal_name'])
        self.assertEqual((service.token_terminal, service.public_key), ('fxpmichalwebtok', 'michal_pk'))
        self.assertEqual(kwargs['token'], 'the-standing-order-card')
        self.assertEqual(lookups, [])

    def test_a_terminal_no_longer_configured_is_not_guessed(self, _email):
        # Paid on the test terminal before the switch: nothing to send, nowhere to send it.
        invoice = self.invoice(tranzila_terminal='realtest')

        result, refund, lookups = self.refund(invoice)

        self.assertFalse(result['success'])
        self.assertIn('realtest', result['error'])
        refund.assert_not_called()
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'completed')

    def test_no_card_details_on_the_report_refunds_nothing(self, _email):
        invoice = self.invoice(tranzila_terminal='cogolive')

        result, refund, _ = self.refund(invoice, report=None)

        self.assertFalse(result['success'])
        refund.assert_not_called()
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'completed')
