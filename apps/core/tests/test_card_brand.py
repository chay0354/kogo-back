"""Brand detection and the blocked-brand gate in validate_card_details."""
from django.test import SimpleTestCase, override_settings

from apps.core.card_validation import (
    CardValidationError,
    card_brand,
    luhn_valid,
    validate_card_details,
)

# Every number here is a public test BIN and passes Luhn — so a rejection can
# only come from the brand gate, never from a malformed number.
DINERS = '30569309025904'
DINERS_36 = '36700102000000'
VISA = '4580458045804580'
MASTERCARD = '5326105300985614'
AMEX = '374245455400126'
JCB = '3530111333300000'


def _details(number: str) -> dict:
    return {
        'card_number': number,
        'expiry_month': 12,
        'expiry_year': 2030,
        'cvv': '123',
    }


class CardBrandTests(SimpleTestCase):
    def test_sample_numbers_are_valid_so_the_brand_gate_is_what_rejects(self):
        for number in (DINERS, DINERS_36, VISA, MASTERCARD, AMEX, JCB):
            self.assertTrue(luhn_valid(number), number)

    def test_brands(self):
        self.assertEqual(card_brand(DINERS), 'diners')
        self.assertEqual(card_brand(DINERS_36), 'diners')
        self.assertEqual(card_brand('38520000023237'), 'diners')
        self.assertEqual(card_brand(VISA), 'visa')
        self.assertEqual(card_brand(MASTERCARD), 'mastercard')
        self.assertEqual(card_brand('2221000000000009'), 'mastercard')
        self.assertEqual(card_brand(AMEX), 'amex')
        self.assertEqual(card_brand(JCB), 'jcb')
        self.assertEqual(card_brand('6011000990139424'), 'discover')

    def test_three_prefix_brands_are_not_confused(self):
        """34/37 Amex, 3528-3589 JCB and 36/38/39 Diners all start with 3."""
        self.assertEqual(card_brand('3400000000000009'), 'amex')
        self.assertEqual(card_brand('3529000000000000'), 'jcb')
        self.assertEqual(card_brand('3600000000000008'), 'diners')

    def test_unknown_bin_is_never_a_brand(self):
        self.assertEqual(card_brand('9999999999999999'), 'unknown')
        self.assertEqual(card_brand(''), 'unknown')
        self.assertEqual(card_brand(None), 'unknown')

    def test_spaces_and_dashes_do_not_change_the_brand(self):
        self.assertEqual(card_brand('3056 9309 0259 04'), 'diners')
        self.assertEqual(card_brand('4580-4580-4580-4580'), 'visa')


@override_settings(BLOCKED_CARD_BRANDS='diners')
class BlockedBrandTests(SimpleTestCase):
    def test_diners_is_refused_with_a_parent_facing_message(self):
        with self.assertRaises(CardValidationError) as ctx:
            validate_card_details(_details(DINERS))
        self.assertIn('דיינרס', str(ctx.exception))
        self.assertIn('לא מקבלים', str(ctx.exception))

    def test_diners_36_is_refused_too(self):
        with self.assertRaises(CardValidationError):
            validate_card_details(_details(DINERS_36))

    def test_accepted_brands_still_pass_and_report_their_brand(self):
        for number, brand in ((VISA, 'visa'), (MASTERCARD, 'mastercard'), (AMEX, 'amex')):
            result = validate_card_details(_details(number))
            self.assertEqual(result['brand'], brand)
            self.assertEqual(result['card_number'], number)

    def test_an_unknown_bin_is_let_through(self):
        """An unrecognised range must never stop a paying customer."""
        result = validate_card_details(_details('9999999999999995'))
        self.assertEqual(result['brand'], 'unknown')

    def test_a_bad_number_still_fails_on_the_number_not_the_brand(self):
        with self.assertRaises(CardValidationError) as ctx:
            validate_card_details(_details('30569309025905'))
        self.assertIn('אינו תקין', str(ctx.exception))


@override_settings(BLOCKED_CARD_BRANDS='')
class NoBlockedBrandsTests(SimpleTestCase):
    def test_empty_setting_accepts_every_brand(self):
        self.assertEqual(validate_card_details(_details(DINERS))['brand'], 'diners')


@override_settings(BLOCKED_CARD_BRANDS='diners, amex')
class MultipleBlockedBrandsTests(SimpleTestCase):
    def test_list_is_parsed_and_whitespace_ignored(self):
        for number in (DINERS, AMEX):
            with self.assertRaises(CardValidationError):
                validate_card_details(_details(number))
        self.assertEqual(validate_card_details(_details(VISA))['brand'], 'visa')
