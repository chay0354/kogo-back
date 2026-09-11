from decimal import Decimal

from django.test import SimpleTestCase

from apps.core.vat import DOCUMENT_TITLE, add_vat, split_vat_inclusive


class AddVatTests(SimpleTestCase):
    def test_adds_vat_to_the_agora(self):
        self.assertEqual(add_vat(Decimal('100')), Decimal('118.00'))
        # 84.75 × 1.18 = 100.005, rounded half up
        self.assertEqual(add_vat(Decimal('84.75')), Decimal('100.01'))
        self.assertEqual(add_vat(0), Decimal('0.00'))
        self.assertEqual(add_vat('1000.00'), Decimal('1180.00'))


class VatSplitTests(SimpleTestCase):
    def test_example_100_ils(self):
        before, vat, gross = split_vat_inclusive(Decimal('100.00'))
        self.assertEqual(gross, Decimal('100.00'))
        self.assertEqual(before, Decimal('84.75'))
        self.assertEqual(vat, Decimal('15.25'))
        self.assertEqual(before + vat, gross)

    def test_document_title(self):
        self.assertEqual(DOCUMENT_TITLE, 'חשבונית מס / קבלה')
