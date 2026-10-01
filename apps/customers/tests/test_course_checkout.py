"""
A course signup paid on Tranzila's hosted page (COURSE_HOSTED_PAGE_ENABLED).

The widget registers as before; the page only checks and saves the card (NK);
the server charges the cart once from the token on cogolivetok and activates
every payment the way a typed-card charge does. Tranzila's notify is public:
only the terminal's report can lead to a charge, and nothing charges twice.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.tests.test_fixtures import TestDataFactory
from apps.core.tranzila_service import TranzilaService
from apps.customers.models import CourseCheckout, Payment, RecurringPayment, TranzilaTransaction

SETTINGS = dict(
    TRANZILA_TERMINAL='cogolive',
    TRANZILA_TOKEN_TERMINAL='cogolivetok',
    TRANZILA_PUBLIC_KEY='cogolive-app-key',
    TRANZILA_SECRET_KEY='cogolive-secret-key',
    TRANZILA_PROD_TERMINAL='fxpmichalweb',
    TRANZILA_PROD_TOKEN_TERMINAL='fxpmichalwebtok',
    TRANZILA_PROD_SUPPLIER='fxpmichalweb',
    TRANZILA_PROD_PUBLIC_KEY='michal-app-key',
    TRANZILA_PROD_SECRET_KEY='michal-secret-key',
    TRANZILA_HOSTED_PAGE_ENABLED=True,
    COURSE_HOSTED_PAGE_ENABLED=True,
    COURSE_TOKEN_TERMINAL='',
    CRM_API_BASE_URL='https://api.example.test',
    CRM_FRONTEND_URL='https://crm.example.test',
    REGISTRATION_FEE_ILS=120,
    SUBSCRIPTION_FIRST_CHARGE_DATE='',
)
START = '/api/v1/customers/widget/checkout/start/'
NOTIFY = '/api/v1/customers/widget/checkout/notify/'
SAVED_TOKEN = 'tok-from-the-report'
CHARGED = {'success': True, 'transaction_id': '9001', 'confirmation_code': '0044444', 'response_code': '000',
           'raw_response': {}}


def _register_payload(**overrides):
    base = {
        'parent_id_number': '123456782', 'parent_first_name': 'Dana', 'parent_last_name': 'Levi',
        'parent_phone': '0501234567', 'parent_email': 'parent@example.com',
        'child_first_name': 'Noa', 'child_last_name': 'Levi', 'child_id_number': '234567892',
        'child_birth_date': '2016-01-01', 'child_gender': 'female',
    }
    base.update(overrides)
    return base


def _no_discount(**kwargs):
    from apps.customers.discount_service import DiscountCalculation

    return DiscountCalculation(applicable_discounts=[], total_discount_amount=Decimal('0.00'),
                               final_price=kwargs['base_price'], base_price=kwargs['base_price'])


def _report_row(checkout, **overrides):
    """The page's row as /v1/transactions has it: agorot, Israel time, a token and its expiry."""
    local = timezone.now().astimezone(ZoneInfo('Asia/Jerusalem'))
    row = {
        'index': '5555', 'tranmode': 'NK', 'processor_response_code': '000',
        'amount': str(int(checkout.page_sum * 100)), 'authorization_number': '0012345',
        'transaction_date': local.strftime('%Y-%m-%d'), 'transaction_time': local.strftime('%H:%M:%S'),
        'credit_card_token': SAVED_TOKEN, 'expiration_month': '09', 'expiration_year': '29',
    }
    row.update(overrides)
    return {'success': True, 'transaction': row}


def _check_row(checkout, **overrides):
    """The NK page's row as cogolive's report really shows it (29.9.2026): N, J2, approval 0000000."""
    fields = {'tranmode': 'N', 'txn_type': 'J2', 'authorization_number': '0000000'}
    fields.update(overrides)
    return _report_row(checkout, **fields)


@override_settings(**SETTINGS)
@patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request', return_value='https://pay.test/x')
@patch('apps.customers.discount_service.DiscountService.evaluate_discounts_for_payment', side_effect=_no_discount)
@patch('apps.core.payment_service.PaymentService._send_registration_whatsapp')
@patch('apps.customers.checkout_invoice.issue_widget_checkout_invoice')
class CourseCheckoutTest(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()  # the start throttle counts across tests otherwise
        self.client = APIClient()
        self.course = TestDataFactory.create_course(price=Decimal('300.00'))
        self.lesson_a = TestDataFactory.create_lesson(course=self.course, day_of_week=0)
        self.lesson_b = TestDataFactory.create_lesson(course=self.course, day_of_week=3)

    # -- helpers -------------------------------------------------------------

    def _register_cart(self):
        first = self.client.post('/api/v1/customers/widget/register/',
                                 _register_payload(course_id=str(self.course.id), lesson_id=str(self.lesson_a.id)),
                                 format='json')
        assert first.status_code == 201, first.content
        child_id = first.json()['child_id']
        second = self.client.post('/api/v1/customers/widget/register/',
                                  _register_payload(course_id=str(self.course.id), lesson_id=str(self.lesson_b.id),
                                                    existing_child_id=child_id),
                                  format='json')
        assert second.status_code == 201, second.content
        return [first.json()['payment_id'], second.json()['payment_id']]

    def _start(self, payment_ids):
        with patch.object(TranzilaService, 'create_handshake_token', return_value='thtk-1'):
            return self.client.post(START, {'payment_ids': payment_ids}, format='json')

    def _notify(self, checkout, *, response='000', index='5555', code='0012345', token='forged-token-in-the-post'):
        return self.client.post(NOTIFY, {
            'Response': response, 'index': index, 'ConfirmationCode': code, 'sum': str(checkout.page_sum),
            'pdesc': str(checkout.id).replace('-', ''), 'ccno': '4580', 'TranzilaTK': token,
        })

    def _paid(self, checkout, *, row=None, charge=None, **notify):
        """Notify with the report and the charge faked; returns the charges sent."""
        sent = []

        def fake_charge(service, **kwargs):
            sent.append({'terminal': service.token_terminal, 'key': service.public_key, **kwargs})
            return dict(charge or CHARGED)

        with patch.object(TranzilaService, 'find_transaction', return_value=row or _report_row(checkout)), \
                patch.object(TranzilaService, 'charge_with_token', autospec=True, side_effect=fake_charge):
            response = self._notify(checkout, **notify)
        return response, sent

    # -- the switch ----------------------------------------------------------

    def test_with_the_switch_off_the_widget_keeps_its_card_form(self, *_):
        ids = self._register_cart()
        with override_settings(COURSE_HOSTED_PAGE_ENABLED=False):
            response = self._start(ids)
        self.assertEqual(response.json(), {'use_card_form': True})
        self.assertFalse(CourseCheckout.objects.exists())

    def test_a_listed_test_course_uses_the_page_while_the_switch_is_off(self, *_):
        ids = self._register_cart()
        with override_settings(COURSE_HOSTED_PAGE_ENABLED=False, COURSE_HOSTED_PAGE_COURSE_IDS=[str(self.course.id)]):
            listed = self._start(ids)
        self.assertIn('url', listed.json())
        with override_settings(COURSE_HOSTED_PAGE_ENABLED=False, COURSE_HOSTED_PAGE_COURSE_IDS=['another-course']):
            other = self._start(ids)
        self.assertEqual(other.json(), {'use_card_form': True})

    def test_the_course_page_opens_by_its_own_switch_while_the_general_one_is_off(self, *_):
        # 30.9.2026: TRANZILA_HOSTED_PAGE_ENABLED is off (store, till, general links).
        ids = self._register_cart()
        with override_settings(TRANZILA_HOSTED_PAGE_ENABLED=False):
            opened = self._start(ids)
        self.assertIn('/cogolive/iframenew.php?', opened.json()['url'])
        with override_settings(TRANZILA_HOSTED_PAGE_ENABLED=False, COURSE_HOSTED_PAGE_ENABLED=False):
            closed = self._start(ids)
        self.assertEqual(closed.json(), {'use_card_form': True})

    # -- the page ------------------------------------------------------------

    def test_the_page_checks_and_saves_the_card_on_cogolive_for_the_cart_sum(self, *_):
        ids = self._register_cart()
        response = self._start(ids)
        self.assertEqual(response.status_code, 200, response.content)
        checkout = CourseCheckout.objects.get(id=response.json()['checkout_id'])
        total = sum(Payment.objects.get(id=pid).final_amount for pid in ids)
        self.assertEqual(checkout.amount, total)
        self.assertEqual(checkout.page_sum, total)
        self.assertEqual((checkout.page_terminal, checkout.token_terminal), ('cogolive', 'cogolivetok'))
        url = response.json()['url']
        self.assertIn('/cogolive/iframenew.php?', url)
        self.assertIn('tranmode=NK', url)
        self.assertIn(f'pdesc={checkout.id.hex}', url)
        self.assertIn('notify_url_address=https%3A%2F%2Fapi.example.test%2Fapi%2Fv1%2Fcustomers%2Fwidget%2Fcheckout%2Fnotify%2F', url)
        self.assertNotIn('bit_pay', url)
        self.assertEqual(set(str(p.id) for p in checkout.payments.all()), set(ids))

    def test_a_new_page_replaces_the_open_one(self, *_):
        ids = self._register_cart()
        first = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        second = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        first.refresh_from_db()
        self.assertEqual(first.status, CourseCheckout.STATUS_REPLACED)
        self.assertEqual(second.status, CourseCheckout.STATUS_PAGE_OPEN)
        # A notify for the replaced page charges nothing.
        _, sent = self._paid(first)
        self.assertEqual(sent, [])

    # -- the charge ----------------------------------------------------------

    def test_a_verified_page_is_charged_once_for_the_cart_and_activates_everything(self, invoice, whatsapp, *_):
        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        response, sent = self._paid(checkout)
        self.assertEqual(response.json()['status'], 'completed', response.content)

        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0]['terminal'], sent[0]['key']), ('cogolivetok', 'cogolive-app-key'))
        self.assertEqual(sent[0]['token'], SAVED_TOKEN)        # from the report, not the POST
        self.assertEqual((sent[0]['expire_month'], sent[0]['expire_year']), (9, 2029))
        self.assertEqual(sent[0]['amount'], checkout.amount)
        self.assertEqual(sent[0]['duplicate_guard_key'], f'checkout-{checkout.id}')

        payments = Payment.objects.filter(id__in=ids)
        self.assertEqual({p.status for p in payments}, {'completed'})
        shared = {p.tranzila_transaction_id for p in payments}
        self.assertEqual(len(shared), 1)
        txn = TranzilaTransaction.objects.get(id=shared.pop())
        self.assertEqual((txn.transaction_id, txn.tranzila_terminal, txn.is_successful), ('9001', 'cogolivetok', True))
        orders = RecurringPayment.objects.filter(initial_payment_id__in=ids)
        self.assertEqual(orders.count(), 2)
        self.assertEqual({(o.tranzila_token, o.tranzila_terminal, o.card_expire_month) for o in orders},
                         {(SAVED_TOKEN, 'cogolivetok', 9)})
        child = payments.first().child
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')
        invoice.assert_called_once()
        self.assertEqual(whatsapp.call_count, 1, 'one message per child, not per lesson')

    def test_a_repeated_notify_charges_nothing_more(self, *_):
        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        _, first = self._paid(checkout)
        _, second = self._paid(checkout)
        self.assertEqual((len(first), len(second)), (1, 0))

    def test_a_forged_notify_without_a_report_row_charges_nothing(self, *_):
        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        _, sent = self._paid(checkout, row={'success': True, 'transaction': None})
        checkout.refresh_from_db()
        self.assertEqual(sent, [])
        self.assertEqual((checkout.status, checkout.review_reason), ('review', 'unverified_page'))

    def test_another_approval_number_charges_nothing(self, *_):
        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        _, sent = self._paid(checkout, row=_report_row(checkout, authorization_number='0099999'))
        self.assertEqual(sent, [])

    def test_money_taken_on_the_page_itself_is_never_charged_again(self, *_):
        # The payer changed tranmode to A in the page address: the sum moved there.
        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        _, sent = self._paid(checkout, row=_report_row(checkout, tranmode='A'))
        checkout.refresh_from_db()
        self.assertEqual(sent, [])
        self.assertEqual((checkout.status, checkout.review_reason), ('review', 'charged_at_page'))

    # -- the card check as the report really shows it (29.9.2026) ---------------

    def test_a_card_check_is_tied_to_its_notify_by_the_card_token_and_charged_once(self, *_):
        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        response, sent = self._paid(checkout, row=_check_row(checkout), code='0000000', token=SAVED_TOKEN)
        self.assertEqual(response.json()['status'], 'completed', response.content)
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0]['terminal'], sent[0]['token']), ('cogolivetok', SAVED_TOKEN))
        checkout.refresh_from_db()
        self.assertEqual((checkout.page_tranmode, checkout.card_token), ('N', SAVED_TOKEN))

    def test_a_card_check_with_another_card_token_charges_nothing(self, *_):
        # Anyone can post a notify quoting another parent's transaction number;
        # only Tranzila and that parent have the token it saved.
        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        _, sent = self._paid(checkout, row=_check_row(checkout), code='0000000', token='someone-elses-token')
        checkout.refresh_from_db()
        self.assertEqual(sent, [])
        self.assertEqual((checkout.status, checkout.review_reason), ('review', 'unverified_page'))

    def test_a_card_check_notify_without_a_token_charges_nothing(self, *_):
        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        _, sent = self._paid(checkout, row=_check_row(checkout), code='0000000', token='')
        checkout.refresh_from_db()
        self.assertEqual(sent, [])
        self.assertEqual(checkout.status, 'review')

    def test_a_card_check_the_poll_finds_first_waits_for_the_notify(self, *_):
        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        status_url = f'/api/v1/customers/widget/checkout/{checkout.id}/?index=5555&code=0000000'
        with patch.object(TranzilaService, 'find_transaction', return_value=_check_row(checkout)), \
                patch.object(TranzilaService, 'charge_with_token') as charge:
            waiting = self.client.get(status_url).json()
            again = self.client.get(status_url).json()
        charge.assert_not_called()
        self.assertEqual((waiting['status'], again['status']), ('page_open', 'page_open'))
        checkout.refresh_from_db()
        self.assertEqual((checkout.page_index, checkout.card_token), ('5555', ''))

        response, sent = self._paid(checkout, row=_check_row(checkout), code='0000000', token=SAVED_TOKEN)
        self.assertEqual(response.json()['status'], 'completed', response.content)
        self.assertEqual(len(sent), 1)

    def test_a_card_check_whose_notify_never_comes_goes_to_the_office(self, *_):
        from apps.core.models import OfficeAlert

        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        checked_at = (timezone.now() - timedelta(minutes=6)).astimezone(ZoneInfo('Asia/Jerusalem'))
        row = _check_row(checkout, transaction_time=checked_at.strftime('%H:%M:%S'),
                         transaction_date=checked_at.strftime('%Y-%m-%d'))
        CourseCheckout.objects.filter(id=checkout.id).update(created_at=timezone.now() - timedelta(minutes=8))
        with patch.object(TranzilaService, 'find_transaction', return_value=row), \
                patch.object(TranzilaService, 'charge_with_token') as charge, \
                self.captureOnCommitCallbacks(execute=True):
            status = self.client.get(f'/api/v1/customers/widget/checkout/{checkout.id}/?index=5555&code=0000000').json()
        charge.assert_not_called()
        self.assertEqual(status['status'], 'review')
        checkout.refresh_from_db()
        self.assertEqual(checkout.review_reason, 'no_notify')
        alert = OfficeAlert.objects.get(kind='course_checkout_no_notify')
        self.assertIn('5555', alert.action)
        self.assertIn('Noa Levi', alert.customer)

    def test_a_page_left_open_with_a_number_is_in_the_morning_brief(self, *_):
        from apps.core.daily_brief import check_course_checkouts

        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        self.assertEqual(check_course_checkouts(timezone.localdate()).count, 0)
        CourseCheckout.objects.filter(id=checkout.id).update(
            page_index='5555', created_at=timezone.now() - timedelta(minutes=31),
        )
        self.assertEqual(check_course_checkouts(timezone.localdate()).count, 1)

    def test_a_declined_card_check_charges_nothing(self, *_):
        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        with patch.object(TranzilaService, 'charge_with_token') as charge:
            self._notify(checkout, response='004')
        charge.assert_not_called()
        checkout.refresh_from_db()
        self.assertEqual(checkout.status, 'declined')

    def test_a_declined_charge_fails_the_payments_and_leaves_a_new_child_in_registration(self, invoice, whatsapp, *_):
        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        declined = {'success': False, 'error': 'Declined', 'response_code': '0', 'message': 'Charge failed: Declined'}
        response, sent = self._paid(checkout, charge=declined)
        self.assertEqual(response.json()['status'], 'declined')
        self.assertEqual({p.status for p in Payment.objects.filter(id__in=ids)}, {'failed'})
        child = Payment.objects.get(id=ids[0]).child
        child.refresh_from_db()
        # A first charge that failed registered nothing: a new child stays in
        # registration (_status_after_failed_charge, #151), as on the card form.
        self.assertEqual(child.status, 'pending')
        self.assertFalse(RecurringPayment.objects.filter(initial_payment_id__in=ids).exists())
        self.assertFalse(TranzilaTransaction.objects.filter(idempotency_key=f'course_checkout_{checkout.id}').exists())
        invoice.assert_not_called()
        whatsapp.assert_not_called()

    def test_no_answer_to_the_charge_keeps_the_claim_and_never_charges_again(self, *_):
        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        timeout = {'success': False, 'uncertain': True, 'error': 'Request timed out', 'response_code': '999'}
        response, sent = self._paid(checkout, charge=timeout)
        self.assertEqual(response.json()['status'], 'uncertain')
        self.assertEqual({p.status for p in Payment.objects.filter(id__in=ids)}, {'processing'})
        self.assertTrue(TranzilaTransaction.objects.filter(
            idempotency_key=f'course_checkout_{checkout.id}', is_successful=False).exists())
        _, again = self._paid(checkout)
        self.assertEqual(again, [])

        from apps.core.daily_brief import check_course_checkouts
        self.assertEqual(check_course_checkouts(timezone.localdate()).severity, 'red')

    def test_a_request_tranzila_rejects_is_not_charged_and_goes_to_the_office(self, *_):
        from apps.core.models import OfficeAlert

        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        rejected = {'success': False, 'error': 'Json does not match validation schema', 'response_code': '20004',
                    'message': 'Charge failed: Json does not match validation schema'}
        with self.captureOnCommitCallbacks(execute=True):
            response, sent = self._paid(checkout, charge=rejected)
        self.assertEqual(response.json()['status'], 'review')
        checkout.refresh_from_db()
        self.assertEqual(checkout.review_reason, 'request_rejected')
        self.assertEqual({p.status for p in Payment.objects.filter(id__in=ids)}, {'pending'})
        child = Payment.objects.get(id=ids[0]).child
        child.refresh_from_db()
        self.assertNotEqual(child.status, 'payment_problem')
        self.assertFalse(TranzilaTransaction.objects.filter(idempotency_key=f'course_checkout_{checkout.id}').exists())
        alert = OfficeAlert.objects.get(kind='course_checkout_rejected')
        self.assertIn('20004', alert.why)

    def test_a_seat_taken_meanwhile_stops_the_charge(self, *_):
        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        with patch('apps.customers.widget_views.precheck_widget_capacity', return_value='השיעור מלא'):
            _, sent = self._paid(checkout)
        checkout.refresh_from_db()
        self.assertEqual(sent, [])
        self.assertEqual(checkout.status, 'failed')
        self.assertEqual({p.status for p in Payment.objects.filter(id__in=ids)}, {'failed'})
        status = self.client.get(f'/api/v1/customers/widget/checkout/{checkout.id}/').json()
        self.assertEqual(status['message'], 'השיעור מלא')

    def test_the_poll_can_finish_a_page_whose_notify_never_came(self, *_):
        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        sent = []

        def fake_charge(service, **kwargs):
            sent.append(kwargs)
            return dict(CHARGED)

        with patch.object(TranzilaService, 'find_transaction', return_value=_report_row(checkout)), \
                patch.object(TranzilaService, 'charge_with_token', autospec=True, side_effect=fake_charge):
            response = self.client.get(f'/api/v1/customers/widget/checkout/{checkout.id}/?index=5555&code=0012345')
        self.assertEqual(response.json()['status'], 'completed')
        self.assertEqual(len(sent), 1)

    def test_the_status_of_an_unknown_checkout_is_404(self, *_):
        self.assertEqual(self.client.get('/api/v1/customers/widget/checkout/not-a-uuid/').status_code, 404)

    # -- the office hears of it at once ---------------------------------------

    def test_no_answer_alerts_the_office_with_the_family_and_what_to_do(self, *_):
        from apps.core.models import OfficeAlert

        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        timeout = {'success': False, 'uncertain': True, 'error': 'Request timed out', 'response_code': '999'}
        with self.captureOnCommitCallbacks(execute=True):
            self._paid(checkout, charge=timeout)
        alert = OfficeAlert.objects.get(kind='course_checkout_uncertain')
        self.assertEqual(alert.title, 'לא ידוע אם ההורה חויב')
        self.assertIn('הרשמה לחוג באתר', alert.where)
        self.assertIn('cogolivetok', alert.where)
        self.assertIn('Request timed out', alert.why)
        for part in ('Dana Levi', '0501234567', 'Noa Levi', self.course.name, str(checkout.amount)):
            self.assertIn(part, alert.customer)
        child_id = Payment.objects.get(id=ids[0]).child_id
        self.assertEqual(alert.link, f'https://crm.example.test/customers?child={child_id}')
        self.assertIn('לבדוק בטרנזילה', alert.action)

    def test_a_full_class_at_the_start_alerts_the_office(self, *_):
        from apps.core.models import OfficeAlert

        ids = self._register_cart()
        with patch('apps.customers.widget_views.precheck_widget_capacity', return_value='השיעור מלא — קיבולת מקסימלית: 20 תלמידים'), \
                self.captureOnCommitCallbacks(execute=True):
            response = self._start(ids)
        self.assertEqual(response.status_code, 400)
        alert = OfficeAlert.objects.get(kind='course_checkout_full')
        self.assertIn('השיעור מלא', alert.why)
        self.assertIn('Noa Levi', alert.customer)

    def test_a_paid_signup_alerts_nobody(self, *_):
        from apps.core.models import OfficeAlert

        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        with self.captureOnCommitCallbacks(execute=True):
            self._paid(checkout)
        self.assertFalse(OfficeAlert.objects.exists())

    # -- afterwards ----------------------------------------------------------

    def test_refunding_one_lesson_of_a_shared_charge_is_a_credit_never_a_cancel(self, *_):
        from apps.core.payment_service import PaymentService

        ids = self._register_cart()
        checkout = CourseCheckout.objects.get(id=self._start(ids).json()['checkout_id'])
        self._paid(checkout)
        refunded = {'success': True, 'transaction_id': '9002', 'confirmation_code': '1', 'response_code': '000'}
        with patch.object(TranzilaService, 'refund_transaction', return_value=refunded) as refund, \
                patch('apps.core.payment_service.PaymentService._issue_payment_credit_note'):
            result = PaymentService().refund_payment(ids[0], reason='ביטול')
        self.assertTrue(result['success'], result)
        kwargs = refund.call_args.kwargs
        self.assertFalse(kwargs['allow_cancel'])
        self.assertFalse(kwargs['prefer_cancel'])
        self.assertEqual(kwargs['terminal_name'], 'cogolivetok')
        self.assertEqual(kwargs['amount'], Payment.objects.get(id=ids[0]).final_amount)
