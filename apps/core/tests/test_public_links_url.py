"""
Links sent to customers open on the customers' own address; the office's stay.

Owner, 1.10.2026: a link sent for collection should not read
kogo-front.vercel.app. PUBLIC_LINKS_URL (pay.cogomelo.co.il) is that address —
for every link a customer receives, and for none of the office's own links,
whose readers are signed in on the CRM address.
"""
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from apps.core.frontend_url import public_frontend_url
from apps.core.password_reset_email import crm_frontend_url
from apps.customers.card_update import card_update_public_url_for_token
from apps.payment_links.models import PaymentLink

CRM = 'https://kogo-front.vercel.app'
PAY = 'https://pay.cogomelo.co.il'


@override_settings(CRM_FRONTEND_URL=CRM, PUBLIC_LINKS_URL=PAY)
class CustomerLinksOpenOnTheirOwnAddressTests(SimpleTestCase):
    def test_the_one_function_every_customer_link_goes_through(self):
        self.assertEqual(public_frontend_url(), PAY)

    def test_a_request_from_the_office_does_not_change_it(self):
        class Request:
            headers = {'Origin': CRM}

        self.assertEqual(public_frontend_url(Request()), PAY)

    def test_payment_and_business_charge_links(self):
        self.assertEqual(PaymentLink(slug='Ab12').public_url(), f'{PAY}/pay/Ab12')

    def test_card_update_links(self):
        self.assertEqual(card_update_public_url_for_token('tok'), f'{PAY}/update-card/tok')

    def test_family_card_replacement_links(self):
        from unittest.mock import patch

        from apps.customers import card_replacement

        with patch.object(card_replacement, 'build_family_token', return_value='fam'):
            self.assertEqual(card_replacement.family_public_url(object()), f'{PAY}/replace-card/fam')

    def test_the_offices_own_links_stay_on_the_crm_address(self):
        self.assertEqual(crm_frontend_url(), CRM)

    def test_a_trailing_slash_does_not_double(self):
        with override_settings(PUBLIC_LINKS_URL=PAY + '/'):
            self.assertEqual(PaymentLink(slug='x').public_url(), f'{PAY}/pay/x')


@override_settings(CRM_FRONTEND_URL=CRM, PUBLIC_LINKS_URL='')
class WithoutTheSettingNothingMovesTests(SimpleTestCase):
    def test_links_stay_on_the_crm_address(self):
        self.assertEqual(public_frontend_url(), CRM)
        self.assertEqual(PaymentLink(slug='Ab12').public_url(), f'{CRM}/pay/Ab12')
        self.assertEqual(card_update_public_url_for_token('tok'), f'{CRM}/update-card/tok')


class TheApiTrustsTheAddressTests(SimpleTestCase):
    def test_the_pages_on_it_may_call_the_api(self):
        # Before a single link points there: the domain is checked first.
        self.assertIn(PAY, settings.CORS_ALLOWED_ORIGINS)
        self.assertIn(PAY, settings.CSRF_TRUSTED_ORIGINS)
