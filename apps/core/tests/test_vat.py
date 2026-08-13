from decimal import Decimal

from django.test import SimpleTestCase

from apps.core.vat import DOCUMENT_TITLE, split_vat_inclusive


class VatSplitTests(SimpleTestCase):
    def test_example_100_ils(self):
        before, vat, gross = split_vat_inclusive(Decimal('100.00'))
        self.assertEqual(gross, Decimal('100.00'))
        self.assertEqual(before, Decimal('84.75'))
        self.assertEqual(vat, Decimal('15.25'))
        self.assertEqual(before + vat, gross)

    def test_document_title(self):
        self.assertEqual(DOCUMENT_TITLE, 'חשבונית מס / קבלה')
