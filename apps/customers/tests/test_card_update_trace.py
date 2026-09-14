"""
The trace a standing-order card link leaves: made, opened, and how it ended.

The rule these tests exist for is the one that is easy to break: the record is
a **log**, never a gate. A token signed before the table existed must still
resolve, still open and still charge with no row anywhere; and no failure while
writing the log may ever turn a card that was charged into an error the parent
retries.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import UserProfile
from apps.customers.card_update import (
    MODE_CARD_ONLY,
    MODE_RENEW,
    apply_new_card,
    build_card_update_token,
    renew_quote,
    resolve_card_update_intent,
)
from apps.customers.models import Payment, RecurringPayment
from apps.payment_links.models import CardUpdateLink

from .test_card_update_modes import (
    CARD,
    TRANZILA_DECLINED,
    TRANZILA_OK,
    TRANZILA_SETTINGS,
    make_sto,
    month_start,
)


def _manager(username='mgr@test.com'):
    user = get_user_model().objects.create_user(username=username, email=username, password='x')
    UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
    return user


def _client_for(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
    return client


@override_settings(**TRANZILA_SETTINGS)
class LinkIsRecordedWhenMadeTests(TestCase):
    def setUp(self):
        self.manager = _manager()
        self.client = _client_for(self.manager)

    def test_the_office_copying_a_renew_link_leaves_a_row_naming_what_it_asks_for(self):
        recurring = make_sto(
            amount='250.00', next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )

        res = self.client.post(
            f'/api/v1/customers/recurring-payments/{recurring.id}/card-update-link/',
            {'mode': MODE_RENEW}, format='json',
        )

        self.assertEqual(res.status_code, 200)
        row = CardUpdateLink.objects.get()
        self.assertEqual(row.recurring_payment_id, recurring.id)
        self.assertEqual(row.child_id, recurring.child_id)
        self.assertEqual(row.mode, MODE_RENEW)
        self.assertEqual(row.amount, Decimal('500.00'))
        self.assertEqual(row.months, [month_start(-1).strftime('%Y-%m'), month_start(0).strftime('%Y-%m')])
        self.assertEqual(row.created_by_id, self.manager.id)
        self.assertEqual(row.status, CardUpdateLink.STATUS_CREATED)
        # The URL handed back is the URL the row remembers.
        self.assertTrue(res.data['url'].endswith(f'/update-card/{row.token}'))

    def test_a_card_only_link_names_no_sum_and_no_months(self):
        recurring = make_sto(status='active', next_billing=month_start(1))

        self.client.post(
            f'/api/v1/customers/recurring-payments/{recurring.id}/card-update-link/',
            {'mode': MODE_CARD_ONLY}, format='json',
        )

        row = CardUpdateLink.objects.get()
        self.assertEqual(row.mode, MODE_CARD_ONLY)
        self.assertIsNone(row.amount)
        self.assertEqual(row.months, [])
        # Copied out of the CRM, never sent by us — so there is no send time.
        self.assertEqual(row.channel, 'copy')
        self.assertIsNone(row.sent_at)

    @patch('apps.customers.card_update.ManyChatService.notify_registration',
           return_value={'sent': True, 'message_id': 'm-1'})
    def test_a_whatsapp_send_records_the_link_and_what_manychat_answered(self, _notify):
        recurring = make_sto(status='failed')

        res = self.client.post(f'/api/v1/customers/recurring-payments/{recurring.id}/send-card-update/')

        self.assertEqual(res.status_code, 200)
        row = CardUpdateLink.objects.get()
        self.assertEqual(row.channel, 'whatsapp')
        self.assertIsNotNone(row.sent_at)
        self.assertEqual(row.sent_result['message_id'], 'm-1')
        self.assertEqual(row.created_by_id, self.manager.id)

    @patch('apps.customers.card_update.ManyChatService.notify_registration',
           return_value={'sent': False, 'reason': 'no_subscriber'})
    def test_a_send_that_failed_still_leaves_the_link_visible_with_no_send_time(self, _notify):
        recurring = make_sto(status='failed')

        self.client.post(f'/api/v1/customers/recurring-payments/{recurring.id}/send-card-update/')

        row = CardUpdateLink.objects.get()
        self.assertIsNone(row.sent_at)
        self.assertEqual(row.sent_result['reason'], 'no_subscriber')
        self.assertEqual(row.status, CardUpdateLink.STATUS_CREATED)

    def test_a_link_the_log_could_not_record_is_still_handed_out(self):
        recurring = make_sto(status='active', next_billing=month_start(1))

        with patch.object(CardUpdateLink.objects, 'create', side_effect=RuntimeError('table gone')):
            res = self.client.post(
                f'/api/v1/customers/recurring-payments/{recurring.id}/card-update-link/',
                {'mode': MODE_CARD_ONLY}, format='json',
            )

        self.assertEqual(res.status_code, 200)
        self.assertIn('/update-card/', res.data['url'])
        self.assertFalse(CardUpdateLink.objects.exists())


@override_settings(**TRANZILA_SETTINGS)
class LinkEndStateTests(TestCase):
    """What the row says after the parent has been to the page."""

    def setUp(self):
        self.client = APIClient()
        self.manager = _manager()

    def _link(self, recurring, *, mode='', amount=None, months=None):
        """The token the office would hand out, and the row beside it."""
        client = _client_for(self.manager)
        if mode == MODE_RENEW:
            quote = renew_quote(recurring)
            amount = quote['amount'] if amount is None else amount
            months = quote['months'] if months is None else months
        res = client.post(
            f'/api/v1/customers/recurring-payments/{recurring.id}/card-update-link/',
            {'mode': mode}, format='json',
        )
        self.assertEqual(res.status_code, 200, res.data)
        row = CardUpdateLink.objects.get(recurring_payment=recurring)
        return row.token, row

    def test_opening_the_page_is_written_down(self):
        recurring = make_sto(status='active', next_billing=month_start(1))
        token, row = self._link(recurring, mode=MODE_CARD_ONLY)

        res = self.client.get(f'/api/v1/customers/card-update/{token}/')

        self.assertEqual(res.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.status, CardUpdateLink.STATUS_OPENED)
        self.assertIsNotNone(row.first_opened_at)

    def test_a_second_visit_does_not_move_the_first_open(self):
        recurring = make_sto(status='active', next_billing=month_start(1))
        token, row = self._link(recurring, mode=MODE_CARD_ONLY)

        self.client.get(f'/api/v1/customers/card-update/{token}/')
        row.refresh_from_db()
        first = row.first_opened_at
        self.client.get(f'/api/v1/customers/card-update/{token}/')

        row.refresh_from_db()
        self.assertEqual(row.first_opened_at, first)

    @patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=TRANZILA_OK)
    def test_a_saved_card_ends_the_link_as_card_saved(self, _verify):
        recurring = make_sto(status='active', next_billing=month_start(1))
        token, row = self._link(recurring, mode=MODE_CARD_ONLY)

        res = self.client.post(
            f'/api/v1/customers/card-update/{token}/charge/', {'card_details': CARD}, format='json',
        )

        self.assertTrue(res.data['success'])
        self.assertFalse(res.data['charged'])
        row.refresh_from_db()
        self.assertEqual(row.status, CardUpdateLink.STATUS_CARD_SAVED)
        self.assertIsNotNone(row.completed_at)
        self.assertIsNone(row.charged_amount)
        # The card really is on the standing order — the log agrees with the money.
        recurring.refresh_from_db()
        self.assertEqual(recurring.tranzila_token, TRANZILA_OK['token'])

    @patch('apps.customers.card_update.PaymentService._create_invoice_from_payment')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_a_charge_ends_the_link_as_charged_for_the_sum_that_was_taken(self, _charge, _receipt):
        recurring = make_sto(
            amount='250.00', next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        token, row = self._link(recurring, mode=MODE_RENEW)

        res = self.client.post(
            f'/api/v1/customers/card-update/{token}/charge/', {'card_details': CARD}, format='json',
        )

        self.assertTrue(res.data['charged'])
        row.refresh_from_db()
        self.assertEqual(row.status, CardUpdateLink.STATUS_CHARGED)
        self.assertEqual(row.charged_amount, Decimal('500.00'))
        self.assertIsNotNone(row.completed_at)
        self.assertIsNotNone(row.first_opened_at)

    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_DECLINED)
    def test_a_declined_card_is_written_down_and_the_link_stays_usable(self, _charge):
        recurring = make_sto(
            amount='250.00', next_billing=month_start(-1), last_charge=month_start(-2) + timedelta(days=1),
        )
        token, row = self._link(recurring, mode=MODE_RENEW)

        res = self.client.post(
            f'/api/v1/customers/card-update/{token}/charge/', {'card_details': CARD}, format='json',
        )

        self.assertEqual(res.status_code, 400)
        row.refresh_from_db()
        self.assertEqual(row.status, CardUpdateLink.STATUS_DECLINED)
        self.assertIn('נדחה', row.last_error)
        self.assertIsNone(row.completed_at)
        # Nothing closed the link: the parent may still try another card.
        self.assertEqual(self.client.get(f'/api/v1/customers/card-update/{token}/').status_code, 200)

    @patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=TRANZILA_OK)
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_DECLINED)
    def test_a_second_card_that_works_replaces_the_decline(self, _charge, _verify):
        recurring = make_sto(status='active', next_billing=month_start(1))
        token, row = self._link(recurring, mode=MODE_CARD_ONLY)
        with patch('apps.core.tranzila_service.TranzilaService.verify_card',
                   return_value={'success': False, 'error': 'הכרטיס נדחה'}):
            self.client.post(
                f'/api/v1/customers/card-update/{token}/charge/', {'card_details': CARD}, format='json',
            )
        row.refresh_from_db()
        self.assertEqual(row.status, CardUpdateLink.STATUS_DECLINED)

        self.client.post(
            f'/api/v1/customers/card-update/{token}/charge/', {'card_details': CARD}, format='json',
        )

        row.refresh_from_db()
        self.assertEqual(row.status, CardUpdateLink.STATUS_CARD_SAVED)
        self.assertEqual(row.last_error, '')


@override_settings(**TRANZILA_SETTINGS)
class TheLogIsNeverAGateTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch('apps.customers.card_update.PaymentService._create_invoice_from_payment')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_a_token_signed_before_this_table_existed_resolves_opens_and_charges(self, _charge, _receipt):
        """
        The exact link already in a parent's WhatsApp: a mode-less token, signed
        by the code that came before, with no row anywhere. It must behave as it
        always did, and the absence of a record must refuse nothing.
        """
        recurring = make_sto(
            amount='250.00', next_billing=month_start(0), last_charge=month_start(-1) + timedelta(days=3),
        )
        token = build_card_update_token(recurring)
        self.assertFalse(CardUpdateLink.objects.exists())

        preview = self.client.get(f'/api/v1/customers/card-update/{token}/')
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.data['will_charge'])

        charge = self.client.post(
            f'/api/v1/customers/card-update/{token}/charge/', {'card_details': CARD}, format='json',
        )

        self.assertTrue(charge.data['success'])
        self.assertTrue(charge.data['charged'])
        # Still no row — the log has nothing to say about a link it never saw,
        # and said nothing about whether the parent could pay.
        self.assertFalse(CardUpdateLink.objects.exists())
        recurring.refresh_from_db()
        self.assertEqual(recurring.status, 'active')
        self.assertEqual(recurring.tranzila_token, TRANZILA_OK['token'])

    @patch('apps.customers.card_update.PaymentService._create_invoice_from_payment')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_a_log_that_cannot_be_written_does_not_fail_the_charge(self, _charge, _receipt):
        recurring = make_sto(
            amount='250.00', next_billing=month_start(0), last_charge=month_start(-1) + timedelta(days=3),
        )
        token = build_card_update_token(recurring)

        with patch.object(CardUpdateLink.objects, 'filter', side_effect=RuntimeError('table gone')):
            res = self.client.post(
                f'/api/v1/customers/card-update/{token}/charge/', {'card_details': CARD}, format='json',
            )

        self.assertTrue(res.data['success'])
        self.assertTrue(res.data['charged'])
        # The money is on record, which is the only thing that had to survive.
        recurring.refresh_from_db()
        self.assertEqual(recurring.tranzila_token, TRANZILA_OK['token'])
        self.assertEqual(
            Payment.objects.filter(child=recurring.child, status='completed')
            .exclude(id=recurring.initial_payment_id).count(),
            1,
        )

    @patch('apps.customers.card_update.PaymentService._create_invoice_from_payment')
    @patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=TRANZILA_OK)
    def test_the_row_is_written_only_after_the_money_is(self, _charge, _receipt):
        """
        The order the receipt already follows. If the log ran first — or inside
        the charge — a log that failed would take the charge down with it.
        """
        recurring = make_sto(
            amount='250.00', next_billing=month_start(0), last_charge=month_start(-1) + timedelta(days=3),
        )
        token = build_card_update_token(recurring)
        seen = {}

        def finished(_token, result):
            seen['charged_payments'] = Payment.objects.filter(
                child=recurring.child, status='completed',
            ).exclude(id=recurring.initial_payment_id).count()
            seen['token_on_order'] = RecurringPayment.objects.get(id=recurring.id).tranzila_token

        with patch('apps.customers.card_update_views.note_card_update_finished', side_effect=finished):
            self.client.post(
                f'/api/v1/customers/card-update/{token}/charge/', {'card_details': CARD}, format='json',
            )

        self.assertEqual(seen['charged_payments'], 1)
        self.assertEqual(seen['token_on_order'], TRANZILA_OK['token'])


@override_settings(**TRANZILA_SETTINGS)
class RecordedLinkOnTheOverviewTests(TestCase):
    """The whole point: after the parent pays, the office can see it happened."""

    @patch('apps.core.tranzila_service.TranzilaService.verify_card', return_value=TRANZILA_OK)
    def test_a_saved_card_shows_up_on_the_links_screen(self, _verify):
        manager = _manager()
        recurring = make_sto(status='active', next_billing=month_start(1))
        crm = _client_for(manager)
        crm.post(
            f'/api/v1/customers/recurring-payments/{recurring.id}/card-update-link/',
            {'mode': MODE_CARD_ONLY}, format='json',
        )
        token = CardUpdateLink.objects.get().token

        APIClient().post(
            f'/api/v1/customers/card-update/{token}/charge/', {'card_details': CARD}, format='json',
        )

        row = crm.get('/api/v1/customers/card-links/').data['results'][0]
        self.assertEqual(row['kind'], 'card_update')
        self.assertEqual(row['status'], CardUpdateLink.STATUS_CARD_SAVED)
        self.assertEqual(row['status_label'], 'כרטיס עודכן')
        self.assertEqual(row['mode_label'], 'שינוי פרטי אשראי')
        self.assertEqual(row['child_name'], recurring.child.full_name)
        self.assertTrue(row['first_opened_at'])
        self.assertTrue(row['completed_at'])
