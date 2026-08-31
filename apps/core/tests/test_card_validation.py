"""Unit tests for card checks that run before a Tranzila charge."""
from datetime import date

from django.test import SimpleTestCase

from apps.core.card_validation import CardValidationError, israeli_id_valid, luhn_valid, validate_card_details
from apps.core.tranzila_service import extract_card_token, invoice_id_from_pdesc, is_mock_credential, pdesc_for_tranzila


class LuhnAndIdTests(SimpleTestCase):
    def test_visa_test_card_passes_luhn(self):
        self.assertTrue(luhn_valid('4580458045804580'))

    def test_obvious_typo_fails_luhn(self):
        self.assertFalse(luhn_valid('4580458045804581'))

    def test_valid_israeli_id(self):
        self.assertTrue(israeli_id_valid('123456782'))

    def test_invalid_israeli_id(self):
        self.assertFalse(israeli_id_valid('123456789'))


class ValidateCardDetailsTests(SimpleTestCase):
    TODAY = date(2026, 8, 17)

    def _valid(self, **overrides):
        payload = {
            'card_number': '4580 4580 4580 4580',
            'expiry_month': 12,
            'expiry_year': 2027,
            'cvv': '123',
            'card_holder_id': '123456782',
        }
        payload.update(overrides)
        return validate_card_details(payload, today=self.TODAY)

    def test_normalizes_spaces_and_two_digit_year(self):
        result = self._valid(expiry_year='27')
        self.assertEqual(result['card_number'], '4580458045804580')
        self.assertEqual(result['expiry_year'], 2027)

    def test_rejects_expired_card(self):
        with self.assertRaises(CardValidationError):
            self._valid(expiry_month=7, expiry_year=2026)

    def test_rejects_bad_cvv(self):
        with self.assertRaises(CardValidationError):
            self._valid(cvv='12')

    def test_rejects_bad_luhn(self):
        with self.assertRaises(CardValidationError):
            self._valid(card_number='4580458045804581')


class TranzilaHelperTests(SimpleTestCase):
    def test_extract_token_from_alternate_keys(self):
        self.assertEqual(
            extract_card_token({'transaction_result': {}}, {'TranzilaTK': 'abc123token'}),
            'abc123token',
        )
        self.assertEqual(extract_card_token({'token': 'from_result'}), 'from_result')
        self.assertEqual(
            extract_card_token({
                'error_code': 0,
                'transaction_result': {'token': 'Ynested4528'},
            }),
            'Ynested4528',
        )

    def test_pdesc_strips_uuid_hyphens_and_roundtrips(self):
        uid = '550e8400-e29b-41d4-a716-446655440000'
        encoded = pdesc_for_tranzila(uid)
        self.assertEqual(encoded, '550e8400e29b41d4a716446655440000')
        self.assertEqual(invoice_id_from_pdesc(encoded), uid)

    def test_mock_credentials_are_detected(self):
        self.assertTrue(is_mock_credential('mock-terminal'))
        self.assertFalse(is_mock_credential('realterminal42'))
