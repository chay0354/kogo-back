"""A receipt that fails never erases the charge it was issued for.

The card is charged before the receipt exists. When the receipt shared the
charge's transaction, a failure there rolled the charge's record back: the
monthly run found the month still due and charged the card again, and the
office or parent saw an error for money already taken. A missing receipt is
issued later by `check_invoices`; a second charge is not undone.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.payment_service import JERUSALEM_TZ, PaymentService
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.card_link import build_card_link_token
from apps.customers.card_update import apply_new_card
from apps.customers.discount_service import DiscountCalculation
from apps.customers.financial_models import Invoice
from apps.customers.models import Payment, RecurringPayment
from apps.customers.recurring_billing import process_due_recurring_charges
from apps.customers.tests.test_card_link import _Base as CardLinkBase
from apps.customers.tests.test_card_update import CARD as UPDATE_CARD
from apps.customers.tests.test_card_update import TRANZILA_OK as UPDATE_OK
from apps.customers.tests.test_card_update import _failed_sto
from apps.enrollments.models import LessonEnrollment
from apps.payment_links.models import CardLink


def _broken_numbering(*args, **kwargs):
    # A real database error inside the receipt's transaction, the way a lock
    # timeout or a cancelled statement arrives — not a Python error beside it.
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1 FROM no_such_table_for_this_test')


def _today():
    return timezone.now().astimezone(JERUSALEM_TZ).date()


def _due_standing_order():
    child = TestDataFactory.create_child()
    lesson = TestDataFactory.create_lesson()
    first = Payment.objects.create(
        child=child,
        family=child.family,
        branch=lesson.course.branch,
        lesson=lesson,
        payment_type='recurring_subscription',
        status='completed',
        base_amount=Decimal('260.00'),
        discount_amount=Decimal('0.00'),
        final_amount=Decimal('260.00'),
        registration_fee=Decimal('0.00'),
        description='מנוי',
    )
    return RecurringPayment.objects.create(
        child=child,
        initial_payment=first,
        tranzila_token='card_token_1',
        card_expire_month=12,
        card_expire_year=2030,
        status='active',
        base_amount=Decimal('260.00'),
        discount_amount=Decimal('0.00'),
        amount=Decimal('260.00'),
        billing_day=1,
        start_date=_today(),
        next_billing_date=_today(),
    )


TOKEN_CHARGE_OK = {
    'success': True,
    'transaction_id': 'TRX_MONTH',
    'confirmation_code': 'CONF3',
    'response_code': '000',
    'raw_response': {},
}


class MonthlyRunTest(TestCase):
    def test_a_failed_receipt_keeps_the_charge_and_the_next_run_does_not_charge_again(self):
        recurring = _due_standing_order()
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   return_value=dict(TOKEN_CHARGE_OK)) as charge, \
                patch('apps.documents.numbering.next_document_number', side_effect=_broken_numbering):
            first = process_due_recurring_charges()
            second = process_due_recurring_charges()

        self.assertEqual(charge.call_count, 1)
        self.assertEqual(first['charged'], 1)
        self.assertEqual(first['failed'], 0)
        self.assertIn('receipt not issued', ' '.join(first['errors']))
        self.assertEqual(second['charged'], 0)

        monthly = Payment.objects.filter(child=recurring.child).exclude(id=recurring.initial_payment_id).get()
        self.assertEqual(monthly.status, 'completed')
        self.assertEqual(monthly.tranzila_transaction.transaction_id, 'TRX_MONTH')
        self.assertFalse(Invoice.objects.filter(payment=monthly).exists())
        recurring.refresh_from_db()
        self.assertEqual(recurring.last_charge_date, _today())
        self.assertGreater(recurring.next_billing_date, _today())

    @patch('apps.customers.subscription_invoice_email.send_subscription_invoice_email', return_value=True)
    def test_the_receipt_is_still_issued_and_emailed_after_the_charge(self, email):
        recurring = _due_standing_order()
        with patch('apps.customers.recurring_billing.TranzilaService.charge_with_token',
                   return_value=dict(TOKEN_CHARGE_OK)), \
                self.captureOnCommitCallbacks(execute=True):
            summary = process_due_recurring_charges()

        self.assertEqual(summary['charged'], 1)
        self.assertEqual(summary['errors'], [])
        monthly = Payment.objects.filter(child=recurring.child).exclude(id=recurring.initial_payment_id).get()
        invoice = Invoice.objects.get(payment=monthly)
        self.assertTrue(invoice.invoice_number.startswith('IR-'))
        email.assert_called_once_with(invoice)


@override_settings(TRANZILA_PROD_SECRET_KEY='test_secret_key')
class CardUpdateTest(TestCase):
    def test_a_failed_receipt_keeps_the_charge_and_the_new_card(self):
        recurring = _failed_sto()
        with patch('apps.core.tranzila_service.TranzilaService.charge_with_card', return_value=dict(UPDATE_OK)), \
                patch('apps.documents.numbering.next_document_number', side_effect=_broken_numbering):
            result = apply_new_card(recurring, dict(UPDATE_CARD))

        self.assertTrue(result['success'])
        self.assertTrue(result['charged'])
        recurring.refresh_from_db()
        self.assertEqual(recurring.status, 'active')
        self.assertEqual(recurring.tranzila_token, 'Ynewtoken4580')
        self.assertEqual(recurring.last_charge_date, _today())
        charge = Payment.objects.filter(child=recurring.child, status='completed').exclude(
            id=recurring.initial_payment_id,
        ).get()
        self.assertIsNotNone(charge.tranzila_transaction)
        self.assertFalse(Invoice.objects.filter(payment=charge).exists())


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class ManualChargeTest(TestCase):
    @patch('apps.core.payment_service.PaymentService._send_registration_whatsapp')
    @patch('apps.core.payment_service.DiscountService.evaluate_discounts_for_payment')
    @patch('apps.core.payment_service.TranzilaService.charge_with_card')
    def test_a_failed_receipt_keeps_the_charge_the_standing_order_and_the_seat(self, charge, discount, _whatsapp):
        discount.return_value = DiscountCalculation(
            applicable_discounts=[],
            total_discount_amount=Decimal('0.00'),
            final_price=Decimal('260.00'),
            base_price=Decimal('260.00'),
        )
        charge.return_value = {
            'success': True,
            'transaction_id': 'TRX_MANUAL',
            'confirmation_code': 'CONF1',
            'token': 'card_token_1',
            'response_code': '000',
            'raw_response': {},
        }
        child = TestDataFactory.create_child()
        lesson = TestDataFactory.create_lesson()

        with patch('apps.documents.numbering.next_document_number', side_effect=_broken_numbering):
            result = PaymentService().charge_subscription_with_card(
                child_id=str(child.id),
                lesson_id=str(lesson.id),
                card_number='4580458045804580',
                expiry_month=12,
                expiry_year=2030,
                cvv='123',
                card_holder_id='123456782',
            )

        self.assertTrue(result['success'])
        self.assertIsNone(result['invoice_number'])
        payment = Payment.objects.get(id=result['payment_id'])
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.tranzila_transaction.transaction_id, 'TRX_MANUAL')
        self.assertTrue(RecurringPayment.objects.filter(initial_payment=payment, status='active').exists())
        self.assertTrue(LessonEnrollment.objects.filter(child=child, lesson=lesson, status='active').exists())
        self.assertFalse(Invoice.objects.filter(payment=payment).exists())


class ReceiptEmailTest(TestCase):
    def _payment(self):
        child = TestDataFactory.create_child()
        lesson = TestDataFactory.create_lesson()
        return Payment.objects.create(
            child=child,
            family=child.family,
            branch=lesson.course.branch,
            lesson=lesson,
            payment_type='recurring_subscription',
            status='completed',
            base_amount=Decimal('260.00'),
            discount_amount=Decimal('0.00'),
            final_amount=Decimal('260.00'),
            registration_fee=Decimal('0.00'),
            description='מנוי',
        )

    @patch('apps.customers.subscription_invoice_email.send_subscription_invoice_email', return_value=True)
    def test_the_email_waits_for_the_commit(self, email):
        payment = self._payment()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            invoice = PaymentService()._create_invoice_from_payment(payment, None)
        email.assert_not_called()
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        email.assert_called_once_with(invoice)

    @patch('apps.customers.subscription_invoice_email.send_subscription_invoice_email', return_value=True)
    def test_a_receipt_rolled_back_is_never_emailed(self, email):
        payment = self._payment()
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    PaymentService()._create_invoice_from_payment(payment, None)
                    raise RuntimeError('what came after the receipt failed')
            except RuntimeError:
                pass
        email.assert_not_called()
        self.assertFalse(Invoice.objects.filter(payment=payment).exists())


class CardLinkRegenerateTest(CardLinkBase):
    def test_a_regenerated_link_gets_its_fourteen_days_again(self):
        link = self._one_time_link()
        old_token = build_card_link_token(link)
        CardLink.objects.filter(id=link.id).update(created_at=timezone.now() - timedelta(days=20))
        public = APIClient()
        self.assertEqual(public.get(f'/api/v1/customers/card-link/{old_token}/').status_code, 400)

        res = self.client.post(f'/api/v1/customers/card-links/{link.id}/regenerate/')
        self.assertEqual(res.status_code, 200, res.content)
        new_token = res.data['public_url'].split('/c/')[1]
        self.assertEqual(public.get(f'/api/v1/customers/card-link/{new_token}/').status_code, 200)
