"""A month the tenant paid at the office — cash, a check, a bank transfer.

The month turns charged and gets the RT receipt a card charge gets, with a
payment line that names the means; the signing package reads that line to send
the original by mail or on paper. Idempotent, refused where the card may have
been charged, and in one commit. The calendar's rental income then counts the
month once, by its receipt. And a card Tranzila charged after the month was
settled another way is named for the office, never refunded or charged by itself.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core import system_audit
from apps.core.models import UserProfile
from apps.documents.models import DocumentPayment, DocumentSeries, FormalDocument, SignedOriginal
from apps.documents.numbering import SERIES_RENTAL
from apps.documents.signing.sources import REASON_CASH, REASON_CHECK_NOT_CROSSED
from apps.documents.tests.signing_support import signing_on
from apps.rental_billing import billing
from apps.rental_billing.models import TenantCharge, TenantStandingOrder
from apps.rental_billing.offline import late_card_charge
from apps.rental_billing.receipt_email import build_rental_receipt_email
from apps.rental_billing.schedule import add_months
from apps.rental_billing.tests.factories import CHARGES_URL, OK_TOKEN_CHARGE, BillingFixture, make_user

Charge = TenantCharge
Order = TenantStandingOrder
OCT = date(2026, 10, 1)
TOTAL = '1456.78'


def offline_url(charge) -> str:
    return f'{CHARGES_URL}{charge.pk}/record-offline-payment/'


def check_details(**fields) -> dict:
    return {
        'number': '000123', 'bank': '12', 'branch': '600', 'account': '456789',
        'date': '2026-10-10', 'crossed': True, **fields,
    }


class OfflineFixture(BillingFixture):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.manager)
        self.today = timezone.localdate()

    def failed_month(self, period=OCT, **fields):
        order = self.active_order(status=Order.STATUS_FAILED, last_error='Card declined')
        charge = self.charge_row(order, period, Charge.STATUS_FAILED, error='Card declined', card_last4='4242', **fields)
        return order, charge

    def pay(self, charge, **body):
        payload = {'method': 'cash', 'amount': TOTAL, **body}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(offline_url(charge), payload, format='json')


@override_settings(RENTAL_BILLING_ENABLED=False)
class OfflinePaymentTests(OfflineFixture, APITestCase):
    """What the office records, and what it may not. Billing is off: no path here reaches Tranzila."""

    def test_cash_on_a_failed_month_is_charged_with_its_rt_receipt(self):
        order, charge = self.failed_month()

        res = self.pay(charge, paid_on=self.today.isoformat(), reference='פנקס 17', note='הביא למשרד')

        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data['created'])
        data = res.data['charge']
        self.assertEqual((data['status'], data['status_label']), ('charged', 'שולם במזומן'))
        number = data['receipt']['document_number']
        self.assertEqual(number, f'RT-{self.today.year}-000001')
        self.assertEqual(DocumentSeries.objects.get(series=SERIES_RENTAL, year=self.today.year).counter, 1)
        self.assertFalse(data['receipt']['issued_late'])
        self.assertEqual(data['offline_payment']['method'], 'cash')
        self.assertEqual(data['offline_payment']['reference'], 'פנקס 17')
        self.assertFalse(data['late_card_charge'])
        self.assertIn('שולם במשרד במזומן', data['resolution_note'])
        self.assertIn('הביא למשרד', data['resolution_note'])
        self.assertTrue(data['resolved_by_name'])

        doc = FormalDocument.objects.get(document_number=number)
        self.assertEqual((doc.document_type, doc.business_customer_id, doc.total_amount),
                         ('combined', self.tenancy.tenant_id, Decimal(TOTAL)))
        self.assertEqual(doc.issued_by, self.manager)
        self.assertIsNotNone(doc.issued_at)
        self.assertIn('הופק במשרד עם רישום תשלום במזומן', doc.internal_notes)
        payment = doc.payments.get()
        self.assertEqual(
            (payment.payment_method, payment.amount, payment.paid_on, payment.reference, payment.card_last_four),
            ('cash', Decimal(TOTAL), self.today, 'פנקס 17', ''),
        )
        # The order moves past the month; its declined card is still declined.
        order.refresh_from_db()
        self.assertEqual((order.status, order.next_charge_date), (Order.STATUS_FAILED, date(2026, 11, 10)))
        self.assertEqual(self.gateway_calls(), 0)

    def test_a_check_keeps_its_details_on_the_receipt(self):
        _order, charge = self.failed_month()

        res = self.pay(charge, method='check', check=check_details(crossed=False))

        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data['charge']['status_label'], "שולם בצ'ק")
        offline = res.data['charge']['offline_payment']
        self.assertEqual(
            (offline['check_number'], offline['check_bank'], offline['check_branch'], offline['check_account'],
             offline['check_date'], offline['check_crossed']),
            ('000123', '12', '600', '456789', '2026-10-10', False),
        )
        payment = DocumentPayment.objects.get(document__document_number=res.data['charge']['receipt']['document_number'])
        self.assertEqual(
            (payment.payment_method, payment.reference, payment.check_bank, payment.check_branch,
             payment.check_account, payment.check_date, payment.check_crossed),
            ('check', '000123', '12', '600', '456789', date(2026, 10, 10), False),
        )

    def test_a_voided_month_paid_by_transfer(self):
        order = self.active_order()
        charge = self.charge_row(order, OCT, Charge.STATUS_REVIEW)
        billing.void_charge(charge, reason='לא נמצאה עסקה בטרנזילה', user=self.manager)

        res = self.pay(charge, method='bank_transfer', reference='AS-998', paid_on=self.today.isoformat())

        self.assertEqual(res.status_code, 201, res.data)
        data = res.data['charge']
        self.assertEqual((data['status'], data['status_label']), ('charged', 'שולם בהעברה'))
        self.assertIn('לא נמצאה עסקה בטרנזילה', data['resolution_note'])
        payment = DocumentPayment.objects.get(document__document_number=data['receipt']['document_number'])
        self.assertEqual((payment.payment_method, payment.reference), ('bank_transfer', 'AS-998'))

    def test_a_second_call_does_nothing(self):
        _order, charge = self.failed_month()
        first = self.pay(charge)
        again = self.pay(charge, method='check', check=check_details(), amount='1')

        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertFalse(again.data['created'])
        self.assertEqual(again.data['charge']['receipt']['document_number'], first.data['charge']['receipt']['document_number'])
        self.assertEqual(FormalDocument.objects.count(), 1)
        self.assertEqual(DocumentPayment.objects.get().payment_method, 'cash')

    def test_a_payment_dated_earlier_marks_the_receipt_late(self):
        _order, charge = self.failed_month()
        paid_on = self.today - timedelta(days=3)

        res = self.pay(charge, paid_on=paid_on.isoformat())

        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data['charge']['receipt']['issued_late'])
        doc = FormalDocument.objects.get()
        self.assertEqual(doc.document_date, self.today)
        self.assertIn(f'התשלום התקבל ב־{paid_on:%d/%m/%Y}', doc.customer_notes)
        self.assertEqual(timezone.localdate(Charge.objects.get(pk=charge.pk).charged_at), paid_on)

    def test_a_month_whose_card_outcome_is_unknown_is_refused(self):
        order = self.active_order()
        review = self.charge_row(order, OCT, Charge.STATUS_REVIEW)
        reserved = self.charge_row(order, date(2026, 11, 1), Charge.STATUS_RESERVED)
        for charge in (review, reserved):
            res = self.pay(charge)
            self.assertEqual(res.status_code, 409, res.data)
            self.assertIn('ייתכן שהכרטיס חויב', res.data['error'])
            charge.refresh_from_db()
            self.assertIsNone(charge.receipt_id)
        self.assertEqual(FormalDocument.objects.count(), 0)

    def test_a_month_a_card_paid_is_refused(self):
        order = self.active_order()
        charged = self.charge_row(order, OCT, Charge.STATUS_CHARGED, transaction_id='T1', charged_at=timezone.now())
        res = self.pay(charged)
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn('כבר חויב בכרטיס', res.data['error'])
        self.assertEqual(FormalDocument.objects.count(), 0)

    def test_the_amount_must_be_the_months(self):
        _order, charge = self.failed_month()
        res = self.pay(charge, amount='1000')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('שונה מסכום החודש', res.data['error'])
        charge.refresh_from_db()
        self.assertEqual(charge.status, Charge.STATUS_FAILED)

    def test_the_form_is_checked_before_anything_is_written(self):
        _order, charge = self.failed_month()
        cases = (
            ({'method': 'credit_card'}, 'אמצעי תשלום'),
            ({'amount': ''}, 'הסכום'),
            ({'paid_on': (self.today + timedelta(days=1)).isoformat()}, 'בעתיד'),
            ({'paid_on': '31/12/2026'}, 'לא תקין'),
            ({'method': 'check', 'check': {'bank': '12'}}, "מספר הצ'ק"),
            ({'method': 'check', 'check': check_details(date='')}, 'תאריך הפירעון'),
        )
        for body, words in cases:
            res = self.pay(charge, **body)
            self.assertEqual(res.status_code, 400, body)
            self.assertIn(words, res.data['error'], body)
        charge.refresh_from_db()
        self.assertEqual((charge.status, charge.receipt_id), (Charge.STATUS_FAILED, None))

    def test_a_receipt_that_cannot_be_numbered_leaves_the_month_as_it_was(self):
        order, charge = self.failed_month()
        with patch('apps.rental_billing.receipts.next_document_number', side_effect=RuntimeError('series locked')):
            with self.assertRaises(RuntimeError):
                self.pay(charge)
        charge.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual((charge.status, charge.receipt_id, charge.resolved_at), (Charge.STATUS_FAILED, None, None))
        self.assertEqual(order.next_charge_date, date(2026, 10, 10))
        self.assertEqual(FormalDocument.objects.count(), 0)

    @override_settings(RENTAL_BILLING_ENABLED=True)
    def test_the_monthly_run_and_the_card_page_never_charge_a_month_paid_at_the_office(self):
        order, charge = self.failed_month()
        self.assertEqual(self.pay(charge).status_code, 201)
        # The card is fixed and the order runs again: October is taken, November is next.
        Order.objects.filter(pk=order.pk).update(status=Order.STATUS_ACTIVE, next_charge_date=date(2026, 10, 10))
        billing.charge_due(today=date(2026, 10, 12))
        self.assertEqual(self.gateway_calls(), 0)
        order.refresh_from_db()
        self.assertEqual(order.next_charge_date, date(2026, 11, 10))
        self.assertEqual(Charge.objects.get(pk=charge.pk).status, Charge.STATUS_CHARGED)

    def test_a_new_order_starts_after_a_month_paid_at_the_office(self):
        from apps.rental_billing.orders import default_start

        _order, charge = self.failed_month(period=date(2026, 9, 1))
        self.assertEqual(self.pay(charge).status_code, 201)
        self.assertEqual(default_start(self.tenancy, date(2026, 9, 11)), date(2026, 10, 1))

    def test_a_partner_and_a_worker_are_refused(self):
        _order, charge = self.failed_month()
        partner = make_user('partner-offline@test', UserProfile.ROLE_PARTNER, branches=[self.branch])
        worker = make_user('worker-offline@test', UserProfile.ROLE_WORKER)
        for user in (partner, worker):
            self.client.force_authenticate(user)
            self.assertEqual(self.pay(charge).status_code, 403)
        charge.refresh_from_db()
        self.assertEqual(charge.status, Charge.STATUS_FAILED)

    def test_the_charges_list_says_how_each_month_was_paid(self):
        order, charge = self.failed_month()
        self.pay(charge, method='check', check=check_details())
        self.charge_row(order, date(2026, 9, 1), Charge.STATUS_FAILED)

        rows = self.client.get(f'{CHARGES_URL}?standing_order={order.pk}').data

        self.assertEqual([row['status_label'] for row in rows], ["שולם בצ'ק", 'נדחה'])
        self.assertEqual(rows[0]['offline_payment']['check_number'], '000123')
        self.assertIsNone(rows[1]['offline_payment'])
        # Signing is off here: no signed original, so nothing to say about its delivery.
        self.assertIsNone(rows[0]['receipt']['delivery'])

    def test_the_email_names_the_means(self):
        _order, charge = self.failed_month()
        self.pay(charge, method='bank_transfer', reference='AS-1')
        charge.refresh_from_db()
        _subject, text, html = build_rental_receipt_email(charge.receipt, charge)
        self.assertIn('שולם בהעברה בנקאית · אסמכתא AS-1', text)
        self.assertNotIn('כרטיס אשראי', text)
        self.assertIn('שולם בהעברה בנקאית', html)


@signing_on(RENTAL_BILLING_ENABLED=False)
class OfflineDeliveryTests(OfflineFixture, APITestCase):
    """The signing package reads the payment line: cash and an unmarked check on paper, a transfer and a crossed check by mail."""

    def delivered(self, charge, **body):
        with patch('apps.rental_billing.receipt_email.send_resend_email', return_value='msg-id') as resend:
            res = self.pay(charge, **body)
        self.assertEqual(res.status_code, 201, res.data)
        number = res.data['charge']['receipt']['document_number']
        return res, SignedOriginal.objects.get(number=number), resend

    def test_cash_goes_on_paper_and_is_not_mailed(self):
        _order, charge = self.failed_month()
        res, row, resend = self.delivered(charge)
        resend.assert_not_called()
        self.assertEqual((row.delivery, row.delivery_reason, row.channel), ('paper', REASON_CASH, 'rental'))
        self.assertIsNotNone(row.signed_at)
        data = self.client.get(f'{CHARGES_URL}{charge.pk}/').data
        self.assertIsNone(data['receipt_emailed_at'])
        self.assertEqual((data['receipt']['delivery']['delivery'], data['receipt']['delivery']['paper_printed_at']),
                         ('paper', None))

    def test_an_unmarked_check_goes_on_paper(self):
        _order, charge = self.failed_month()
        _res, row, resend = self.delivered(charge, method='check', check=check_details(crossed=False))
        resend.assert_not_called()
        self.assertEqual((row.delivery, row.delivery_reason), ('paper', REASON_CHECK_NOT_CROSSED))

    def test_a_crossed_check_is_mailed_as_its_signed_original(self):
        _order, charge = self.failed_month()
        _res, row, resend = self.delivered(charge, method='check', check=check_details(crossed=True))
        resend.assert_called_once()
        self.assertEqual(row.delivery, 'email')
        self.assertIsNotNone(Charge.objects.get(pk=charge.pk).receipt_emailed_at)

    def test_a_transfer_is_mailed_and_the_screen_says_when(self):
        _order, charge = self.failed_month()
        _res, row, resend = self.delivered(charge, method='bank_transfer', reference='AS-5')
        resend.assert_called_once()
        self.assertIn('שולם בהעברה בנקאית', resend.call_args.kwargs['text'])
        self.assertEqual(row.delivery, 'email')
        data = self.client.get(f'{CHARGES_URL}{charge.pk}/').data
        self.assertIsNotNone(data['receipt_emailed_at'])
        self.assertEqual(data['receipt']['delivery']['delivery'], 'email')
        self.assertIsNotNone(data['receipt']['delivery']['sent_at'])


@override_settings(RENTAL_BILLING_ENABLED=True)
class LateCardChargeTests(OfflineFixture, APITestCase):
    """Tranzila says yes after the office decided: kept, named, never refunded or charged by itself."""

    def voided(self):
        order = self.active_order()
        charge = self.charge_row(order, OCT, Charge.STATUS_REVIEW)
        billing.void_charge(charge, reason='לא נמצאה עסקה', user=self.manager)
        return order, charge

    def test_a_yes_after_a_void_is_kept_and_shown(self):
        _order, charge = self.voided()

        outcome = billing.record_result(charge.pk, dict(OK_TOKEN_CHARGE))

        self.assertEqual(outcome, billing.OUTCOME_LATE)
        charge.refresh_from_db()
        self.assertEqual((charge.status, charge.transaction_id, charge.receipt_id), (Charge.STATUS_VOIDED, 'T100', None))
        self.assertIn(billing.LATE_CHARGE_NOTE, charge.error)
        self.assertTrue(late_card_charge(charge))
        data = self.client.get(f'{CHARGES_URL}{charge.pk}/').data
        self.assertTrue(data['late_card_charge'])
        self.assertEqual(system_audit.probe_rental_late_charges().severity, 'red')
        # Money is not taken a second time on top of it.
        res = self.pay(charge)
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn('T100', res.data['error'])
        self.assertEqual(FormalDocument.objects.count(), 0)
        self.assertEqual(self.gateway_calls(), 0)

    def test_the_office_keeps_it_by_marking_it_charged_with_tranzilas_id(self):
        _order, charge = self.voided()
        billing.record_result(charge.pk, dict(OK_TOKEN_CHARGE))
        url = f'{CHARGES_URL}{charge.pk}/mark-charged/'

        wrong = self.client.post(url, {'transaction_id': 'T999'}, format='json')
        self.assertEqual(wrong.status_code, 400, wrong.data)
        self.assertIn('T100', wrong.data['error'])

        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(url, {'transaction_id': 'T100'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data['status'], res.data['confirmation_code']), ('charged', 'C100'))
        self.assertTrue(res.data['receipt']['document_number'].startswith('RT-'))
        self.assertFalse(res.data['late_card_charge'])
        self.assertEqual(DocumentPayment.objects.get().payment_method, 'credit_card')
        self.assertEqual(system_audit.probe_rental_late_charges().severity, 'green')

    def test_a_yes_after_the_month_was_paid_at_the_office_is_named_as_paid_twice(self):
        _order, charge = self.voided()
        self.assertEqual(self.pay(charge).status_code, 201)

        outcome = billing.record_result(charge.pk, dict(OK_TOKEN_CHARGE))

        self.assertEqual(outcome, billing.OUTCOME_LATE)
        charge.refresh_from_db()
        self.assertEqual((charge.status, charge.transaction_id), (Charge.STATUS_CHARGED, 'T100'))
        self.assertEqual(DocumentPayment.objects.get().payment_method, 'cash')
        self.assertTrue(self.client.get(f'{CHARGES_URL}{charge.pk}/').data['late_card_charge'])
        self.assertEqual(system_audit.probe_rental_late_charges().severity, 'red')
        # Only the office can decide on it: marking it charged again is refused.
        again = self.client.post(f'{CHARGES_URL}{charge.pk}/mark-charged/', {'transaction_id': 'T100'}, format='json')
        self.assertEqual(again.status_code, 400)

    def test_a_card_charge_is_never_named_late(self):
        order = self.active_order(next_charge_date=date(2026, 10, 10))
        billing.charge_due(today=date(2026, 10, 10))
        charge = Charge.objects.get(standing_order=order)
        self.assertEqual(charge.status, Charge.STATUS_CHARGED)
        self.assertFalse(late_card_charge(charge))
        self.assertEqual(system_audit.probe_rental_late_charges().severity, 'green')


@override_settings(RENTAL_BILLING_ENABLED=False)
class CalendarIncomeCountedOnceTests(OfflineFixture, APITestCase):
    """A month with a receipt, card or offline, is its document's income; a voided month with none stays undocumented."""

    def setUp(self):
        super().setUp()
        from apps.documents.period_report import month_bounds
        from apps.rentals.tests.factories import make_rental

        self.period = self.today.replace(day=1)
        self.bounds = month_bounds(self.today.year, self.today.month)
        make_rental(
            self.branch, price='400', event_type='one_time', event_date=self.period,
            renter_name='סטודיו אור', tenancy=self.tenancy, contract=(self.period, add_months(self.period, 12)),
        )

    def calendar(self):
        from apps.documents.undocumented_income import _rental_rows
        from apps.scheduling.studio_rental_finance import aggregate_studio_rental_revenue

        rental = aggregate_studio_rental_revenue(*self.bounds)
        return rental, _rental_rows(None, *self.bounds)

    def test_a_month_paid_at_the_office_is_its_receipts_income(self):
        _order, charge = self.failed_month(period=self.period)
        rental, undocumented = self.calendar()
        self.assertEqual([row.amount for row in rental['rows']], [Decimal('400')])
        self.assertEqual(len(undocumented), 1)

        self.assertEqual(self.pay(charge).status_code, 201)

        rental, undocumented = self.calendar()
        self.assertEqual(rental['rows'], [])
        self.assertEqual(undocumented, [])
        # The branch panel's figure is the calendar's, as before.
        self.assertEqual(rental['total'], Decimal('400'))

    def test_a_voided_month_with_no_receipt_stays_undocumented(self):
        order = self.active_order()
        charge = self.charge_row(order, self.period, Charge.STATUS_REVIEW)
        billing.void_charge(charge, reason='שולם בדרך אחרת', user=self.manager)

        rental, undocumented = self.calendar()

        self.assertEqual([row.amount for row in rental['rows']], [Decimal('400')])
        self.assertEqual(len(undocumented), 1)

    def test_a_card_charge_is_counted_once_too(self):
        order = self.active_order()
        self.charge_row(order, self.period, Charge.STATUS_CHARGED, transaction_id='T1', charged_at=timezone.now())
        self.assertTrue(billing.issue_receipt_safely(Charge.objects.get().pk))
        rental, undocumented = self.calendar()
        self.assertEqual((rental['rows'], undocumented), ([], []))
