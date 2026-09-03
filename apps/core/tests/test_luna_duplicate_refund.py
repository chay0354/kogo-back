from decimal import Decimal

from django.test import SimpleTestCase

from apps.core.luna_duplicate_refund import KEEP_PAYMENT_IDS, REFUND_PAYMENTS


class LunaDuplicateRefundAllowlistTest(SimpleTestCase):
    def test_never_refunds_the_legitimate_charges(self):
        refund_ids = {payment_id for payment_id, _amount, _reason in REFUND_PAYMENTS}
        self.assertTrue(refund_ids.isdisjoint(KEEP_PAYMENT_IDS))
        self.assertEqual(
            {amount for _payment_id, amount, _reason in REFUND_PAYMENTS},
            {Decimal('120.00'), Decimal('225.00')},
        )
