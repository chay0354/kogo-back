"""
A business charge unpaid for a day gets an open tax invoice; paid sooner, one invoice-receipt.

Owner, 1.10.2026: nothing is issued when the link is created. Paid the same
day — a חשבונית מס/קבלה. A full day without payment — a חשבונית מס, and the
payment that follows closes it with a קבלה. Beside it: a payment the customer
started and never finished no longer blocks the link for good.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Business, BusinessCategory, UserProfile
from apps.customers.models import BusinessCustomer, TranzilaTransaction
from apps.documents.models import DocumentSettlement, FormalDocument
from apps.documents.settlement import balance_of
from apps.payment_links.business_charge import (
    issue_business_charge_document,
    issue_overdue_business_invoices,
)
from apps.payment_links.models import PaymentLink, PaymentLinkOption, PaymentLinkPayment

CREATE = '/api/v1/payment-links/links/business-charge/'
CALLBACK = '/api/v1/payment-links/public/callback/'
CRON = '/api/v1/payment-links/cron/business-invoices/'
PAGE = 'https://direct.tranzila.com/cogolive/iframenew.php'


def user(role, email):
    row = get_user_model().objects.create_user(username=email, email=email, password='x')
    UserProfile.objects.update_or_create(user=row, defaults={'role': role})
    return row


@override_settings(
    TRANZILA_BILLING_TERMINAL='',
    DOCUMENT_SIGNING_ENABLED=False,
    BUSINESS_CHARGE_ENABLED=True,
    BUSINESS_CHARGE_INVOICE_AFTER_HOURS=24,
    BUSINESS_CHARGE_ATTEMPT_ABANDONED_MINUTES=20,
    BUSINESS_CHARGE_INVOICE_LINKS_FROM='',
    CRM_API_BASE_URL='https://api.example.test',
    TRANZILA_TERMINAL='general-hosted',
    BUSINESS_CHARGE_TRANZILA_TERMINAL='cogolive',
    TRANZILA_PUBLIC_KEY='pk-business-charge',
    TRANZILA_SECRET_KEY='sk-business-charge',
)
class _Base(TestCase):
    def setUp(self):
        cache.clear()
        self.manager = user(UserProfile.ROLE_MANAGER, 'deferred-invoice@test.local')
        self.branch = Branch.objects.create(name='מרכז')
        self.business = Business.objects.create(name='אירועים')
        self.category = BusinessCategory.objects.create(business=self.business, name='סדנאות')
        self.customer = BusinessCustomer.objects.create(
            first_name='חברת', last_name='בדיקה', company_number='515151515',
            phone='0501234567', email='payer@example.test', business=self.business,
            business_category=self.category, branch=self.branch,
        )
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=self.manager).key}')
        self.public = APIClient()

    def create_link(self, *, hours_old=0, **extra):
        data = {
            'business_customer_id': str(self.customer.id),
            'business_id': str(self.business.id),
            'business_category_id': str(self.category.id),
            'branch_id': str(self.branch.id),
            'amount': '118.00',
            'description': 'סדנה חד־פעמית',
        }
        data.update(extra)
        response = self.client.post(CREATE, data, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        link = PaymentLink.objects.get(pk=response.data['id'])
        if hours_old:
            self.age(link, hours=hours_old)
        return link

    def age(self, row, **delta):
        """Move a row's creation back; `created_at` is auto_now_add, so through the queryset."""
        type(row).objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(**delta))
        row.refresh_from_db()

    def payment(self, link, status=PaymentLinkPayment.STATUS_COMPLETED, **extra):
        option = link.options.get()
        fields = dict(
            link=link, option=option, option_label=option.label, amount=option.amount,
            payer_name=self.customer.full_name, payer_phone=self.customer.phone, payer_email=self.customer.email,
            status=status, gateway_transaction_id='77', gateway_confirmation_code='0001234',
            card_last4='4580', card_type='2', tranzila_terminal='cogolive',
        )
        fields.update(extra)
        return PaymentLinkPayment.objects.create(**fields)

    def tax_invoices(self):
        return FormalDocument.objects.filter(document_type='tax_invoice')

    def start(self, link):
        with patch('apps.core.tranzila_service.TranzilaService.create_payment_request', return_value=PAGE):
            return self.public.post(
                f'/api/v1/payment-links/public/{link.slug}/start/',
                {'option_id': str(link.options.get().id)}, format='json',
            )

    def approved_notify(self, row, *, index):
        """Tranzila's approved notify for `row`, confirmed by the terminal's report."""
        def report(service, asked):
            local = timezone.now().astimezone(ZoneInfo('Asia/Jerusalem'))
            return {'success': True, 'transaction': {
                'index': asked, 'amount': str(int(row.amount * 100)), 'processor_response_code': '000',
                'tranmode': 'A', 'authorization_number': '0000123',
                'transaction_date': local.strftime('%Y-%m-%d'), 'transaction_time': local.strftime('%H:%M:%S'),
            }}

        with patch('apps.core.tranzila_service.TranzilaService.find_transaction', autospec=True, side_effect=report), \
                self.captureOnCommitCallbacks(execute=True):
            return self.public.post(CALLBACK, {
                'Response': '000', 'sum': str(row.amount), 'index': index, 'ConfirmationCode': '0000123',
                'ccno': '4580', 'cardtype': '2', 'pdesc': str(row.id).replace('-', ''),
            })


class OverdueInvoiceTests(_Base):
    def test_a_link_younger_than_a_day_gets_no_invoice(self):
        self.create_link(hours_old=23)
        summary = issue_overdue_business_invoices()
        self.assertEqual((summary['checked'], summary['issued'], summary['errors']), (0, [], []))
        self.assertFalse(self.tax_invoices().exists())

    def test_a_link_unpaid_for_a_day_gets_one_open_tax_invoice_tied_to_it(self):
        link = self.create_link(hours_old=25)
        summary = issue_overdue_business_invoices()
        invoice = self.tax_invoices().get()
        link.refresh_from_db()
        self.assertEqual(link.target_invoice, invoice)
        self.assertTrue(link.is_active)
        self.assertTrue(link.is_open())
        self.assertEqual(summary['issued'], [{
            'link_id': str(link.id), 'document_number': invoice.document_number, 'total': '118.00',
        }])
        self.assertEqual((summary['checked'], summary['skipped'], summary['errors']), (1, 0, []))
        # The link's sum is what the customer pays: VAT is inside it.
        self.assertEqual(
            (invoice.subtotal, invoice.vat_amount, invoice.total_amount),
            (Decimal('100.00'), Decimal('18.00'), Decimal('118.00')),
        )
        self.assertTrue(invoice.prices_include_vat)
        self.assertEqual(invoice.client_type, 'business')
        self.assertEqual(invoice.business_customer, self.customer)
        self.assertEqual((invoice.business, invoice.business_category, invoice.branch),
                         (self.business, self.category, self.branch))
        self.assertEqual(invoice.issued_by, self.manager)
        line = invoice.line_items.get()
        self.assertEqual((line.description, line.quantity, line.unit_price),
                         ('סדנה חד־פעמית', Decimal('1'), Decimal('118.00')))
        self.assertFalse(invoice.payments.exists())
        self.assertEqual(balance_of(invoice).open, Decimal('118.00'))

    def test_now_decides_which_links_are_a_day_old(self):
        self.create_link()
        self.assertEqual(issue_overdue_business_invoices(now=timezone.now() + timedelta(hours=23))['issued'], [])
        self.assertEqual(len(issue_overdue_business_invoices(now=timezone.now() + timedelta(hours=25))['issued']), 1)

    @override_settings(BUSINESS_CHARGE_INVOICE_AFTER_HOURS=48)
    def test_the_waiting_time_is_a_setting(self):
        self.create_link(hours_old=25)
        self.assertEqual(issue_overdue_business_invoices()['issued'], [])
        self.assertFalse(self.tax_invoices().exists())

    def test_a_second_run_issues_nothing_more(self):
        link = self.create_link(hours_old=25)
        first = issue_overdue_business_invoices()
        second = issue_overdue_business_invoices()
        self.assertEqual(len(first['issued']), 1)
        self.assertEqual((second['checked'], second['issued'], second['errors']), (0, [], []))
        self.assertEqual(self.tax_invoices().count(), 1)
        link.refresh_from_db()
        self.assertEqual(link.target_invoice, self.tax_invoices().get())

    def test_paid_in_review_closed_and_expired_links_are_left_alone(self):
        paid = self.create_link(hours_old=25)
        self.payment(paid)
        review = self.create_link(hours_old=25)
        self.payment(review, status=PaymentLinkPayment.STATUS_REVIEW, gateway_transaction_id='78')
        closed = self.create_link(hours_old=25)
        PaymentLink.objects.filter(pk=closed.pk).update(is_active=False)
        expired = self.create_link(hours_old=25)
        PaymentLink.objects.filter(pk=expired.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        general = PaymentLink.objects.create(title='קישור כללי', business=self.business, created_by=self.manager)
        PaymentLinkOption.objects.create(link=general, label='כרטיס', amount=Decimal('50'))
        self.age(general, hours=25)

        summary = issue_overdue_business_invoices()
        self.assertEqual((summary['issued'], summary['errors']), ([], []))
        self.assertFalse(self.tax_invoices().exists())
        self.assertFalse(PaymentLink.objects.filter(target_invoice__isnull=False).exists())

    def test_a_link_that_already_closes_an_invoice_gets_no_other(self):
        existing = FormalDocument.objects.create(
            document_number='TI-TEST-9', document_type='tax_invoice', client_type='business',
            business_customer=self.customer, business=self.business, business_category=self.category,
            branch=self.branch, document_date=timezone.localdate(), subtotal=Decimal('100'),
            vat_amount=Decimal('18'), total_amount=Decimal('118'),
        )
        link = self.create_link(hours_old=25, target_invoice_id=str(existing.id))
        self.assertEqual(issue_overdue_business_invoices()['issued'], [])
        self.assertEqual(self.tax_invoices().count(), 1)
        link.refresh_from_db()
        self.assertEqual(link.target_invoice, existing)

    def test_a_payment_under_way_holds_the_invoice_back(self):
        link = self.create_link(hours_old=25)
        self.payment(link, status=PaymentLinkPayment.STATUS_PENDING)
        summary = issue_overdue_business_invoices()
        self.assertEqual((summary['checked'], summary['skipped'], summary['issued']), (1, 1, []))
        self.assertFalse(self.tax_invoices().exists())

    def test_a_payment_started_long_ago_and_never_finished_does_not(self):
        link = self.create_link(hours_old=25)
        attempt = self.payment(link, status=PaymentLinkPayment.STATUS_PENDING)
        self.age(attempt, minutes=21)
        summary = issue_overdue_business_invoices()
        self.assertEqual(len(summary['issued']), 1)
        link.refresh_from_db()
        self.assertEqual(link.target_invoice, self.tax_invoices().get())
        # The cron writes nothing on the attempt: its notify may still arrive.
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentLinkPayment.STATUS_PENDING)

    def test_one_link_that_cannot_be_invoiced_does_not_stop_the_others(self):
        broken = self.create_link(hours_old=30)
        PaymentLink.objects.filter(pk=broken.pk).update(business_category=None)
        good = self.create_link(hours_old=25)
        summary = issue_overdue_business_invoices()
        self.assertEqual([row['link_id'] for row in summary['issued']], [str(good.id)])
        self.assertEqual([row['link_id'] for row in summary['errors']], [str(broken.id)])
        self.assertIn('עסק וקטגוריה', summary['errors'][0]['error'])
        self.assertEqual(self.tax_invoices().count(), 1)
        # Repaired, the next run issues it.
        PaymentLink.objects.filter(pk=broken.pk).update(business_category=self.category)
        again = issue_overdue_business_invoices()
        self.assertEqual([row['link_id'] for row in again['issued']], [str(broken.id)])
        self.assertEqual(self.tax_invoices().count(), 2)

    def test_a_refused_document_leaves_no_number_and_no_tie(self):
        link = self.create_link(hours_old=25)
        with patch(
            'apps.payment_links.business_charge.document_service.create_invoice',
            side_effect=ValueError('כלל מסמכים'),
        ):
            summary = issue_overdue_business_invoices()
        self.assertEqual(summary['issued'], [])
        self.assertEqual(summary['errors'], [{'link_id': str(link.id), 'error': 'כלל מסמכים'}])
        link.refresh_from_db()
        self.assertIsNone(link.target_invoice)
        self.assertFalse(self.tax_invoices().exists())

    def test_limit_bounds_the_invoices_of_one_run(self):
        for hours in (30, 29, 28):
            self.create_link(hours_old=hours)
        self.assertEqual(len(issue_overdue_business_invoices(limit=2)['issued']), 2)
        self.assertEqual(len(issue_overdue_business_invoices(limit=2)['issued']), 1)
        self.assertEqual(self.tax_invoices().count(), 3)

    def test_links_from_before_the_rule_are_never_invoiced(self):
        self.create_link(hours_old=72)
        recent = self.create_link(hours_old=25)
        bound = (timezone.now() - timedelta(hours=48)).isoformat()
        with override_settings(BUSINESS_CHARGE_INVOICE_LINKS_FROM=bound):
            summary = issue_overdue_business_invoices()
        self.assertEqual([row['link_id'] for row in summary['issued']], [str(recent.id)])
        self.assertEqual(self.tax_invoices().count(), 1)

    @override_settings(BUSINESS_CHARGE_INVOICE_LINKS_FROM='yesterday')
    def test_an_unreadable_start_of_the_rule_issues_nothing(self):
        self.create_link(hours_old=25)
        summary = issue_overdue_business_invoices()
        self.assertEqual(summary['issued'], [])
        self.assertEqual(len(summary['errors']), 1)
        self.assertFalse(self.tax_invoices().exists())

    @override_settings(BUSINESS_CHARGE_ENABLED=False)
    def test_nothing_runs_while_business_charges_are_switched_off(self):
        self.create_link(hours_old=25)
        summary = issue_overdue_business_invoices()
        self.assertTrue(summary['disabled'])
        self.assertEqual(summary['issued'], [])
        self.assertFalse(self.tax_invoices().exists())


class PaymentAroundTheInvoiceTests(_Base):
    def test_a_payment_after_the_invoice_is_a_receipt_that_closes_it(self):
        link = self.create_link(hours_old=25)
        issue_overdue_business_invoices()
        invoice = self.tax_invoices().get()
        payment = self.payment(link)
        document = issue_business_charge_document(payment.id)
        self.assertEqual(document.document_type, 'receipt')
        self.assertEqual(document.total_amount, Decimal('118.00'))
        self.assertEqual(document.linked_document_number, invoice.document_number)
        self.assertEqual(document.payments.get().card_last_four, '4580')
        self.assertEqual(balance_of(invoice).open, Decimal('0.00'))
        settlement = DocumentSettlement.objects.get()
        self.assertEqual((settlement.payer, settlement.invoice, settlement.amount),
                         (document, invoice, Decimal('118.00')))
        self.assertFalse(FormalDocument.objects.filter(document_type='combined').exists())
        self.assertEqual(self.tax_invoices().count(), 1)
        link.refresh_from_db()
        payment.refresh_from_db()
        self.assertFalse(link.is_active)
        self.assertEqual(payment.formal_document, document)
        # And the cron has nothing left to do with it.
        self.assertEqual(issue_overdue_business_invoices()['issued'], [])
        self.assertEqual(self.tax_invoices().count(), 1)

    def test_a_payment_before_the_invoice_is_one_invoice_receipt_and_no_tax_invoice(self):
        link = self.create_link()
        payment = self.payment(link)
        document = issue_business_charge_document(payment.id)
        self.assertEqual(document.document_type, 'combined')
        self.assertEqual(document.total_amount, Decimal('118.00'))
        summary = issue_overdue_business_invoices(now=timezone.now() + timedelta(hours=30))
        self.assertEqual((summary['issued'], summary['errors']), ([], []))
        self.assertFalse(self.tax_invoices().exists())
        link.refresh_from_db()
        self.assertIsNone(link.target_invoice)

    def test_the_document_reads_the_invoice_tied_since_the_payment_was_loaded(self):
        """The payment's own copy of the link is older than the cron's invoice: the locked link decides."""
        link = self.create_link(hours_old=25)
        attempt = self.payment(link, status=PaymentLinkPayment.STATUS_PENDING)
        self.age(attempt, minutes=21)
        stale = PaymentLinkPayment.objects.select_related('link').get(pk=attempt.pk)
        self.assertIsNone(stale.link.target_invoice_id)
        issue_overdue_business_invoices()
        PaymentLinkPayment.objects.filter(pk=attempt.pk).update(status=PaymentLinkPayment.STATUS_COMPLETED)
        document = issue_business_charge_document(stale.id)
        self.assertEqual(document.document_type, 'receipt')
        self.assertEqual(balance_of(self.tax_invoices().get()).open, Decimal('0.00'))
        self.assertFalse(FormalDocument.objects.filter(document_type='combined').exists())

    def test_a_paid_link_whose_document_failed_is_not_invoiced_beside_the_payment(self):
        link = self.create_link(hours_old=25)
        self.payment(link, document_error='הפקה נכשלה')
        self.assertEqual(issue_overdue_business_invoices()['issued'], [])
        self.assertFalse(self.tax_invoices().exists())

    def test_a_second_payment_on_the_link_gets_no_second_document(self):
        link = self.create_link()
        first = self.payment(link)
        second = self.payment(link, gateway_transaction_id='78')
        document = issue_business_charge_document(first.id)
        from apps.payment_links.business_charge import (
            BusinessChargeDocumentError,
            ensure_business_charge_document,
        )

        with self.assertRaises(BusinessChargeDocumentError):
            issue_business_charge_document(second.id)
        ensure_business_charge_document(second.id)
        second.refresh_from_db()
        self.assertIsNone(second.formal_document)
        self.assertIn(document.document_number, second.document_error)
        self.assertEqual(FormalDocument.objects.count(), 1)
        # The first stays as it was, and asking again returns its document.
        self.assertEqual(issue_business_charge_document(first.id).id, document.id)


class AbandonedAttemptTests(_Base):
    def test_an_attempt_under_way_still_blocks_a_second_one(self):
        link = self.create_link()
        self.assertEqual(self.start(link).status_code, 200)
        again = self.start(link)
        self.assertEqual(again.status_code, 409)
        self.assertIn('20', again.data['error'])
        self.assertEqual(PaymentLinkPayment.objects.filter(link=link).count(), 1)

    def test_an_abandoned_attempt_is_retired_and_a_new_one_goes_through(self):
        link = self.create_link()
        first = PaymentLinkPayment.objects.get(pk=self.start(link).data['payment_id'])
        self.age(first, minutes=21)
        again = self.start(link)
        self.assertEqual(again.status_code, 200, again.data)
        first.refresh_from_db()
        self.assertEqual((first.status, first.failure_reason), (PaymentLinkPayment.STATUS_FAILED, 'abandoned'))
        second = PaymentLinkPayment.objects.get(pk=again.data['payment_id'])
        self.assertEqual(second.status, PaymentLinkPayment.STATUS_PENDING)
        self.assertEqual(second.amount, Decimal('118.00'))

    @override_settings(BUSINESS_CHARGE_ATTEMPT_ABANDONED_MINUTES=5)
    def test_the_waiting_time_of_an_attempt_is_a_setting(self):
        link = self.create_link()
        first = PaymentLinkPayment.objects.get(pk=self.start(link).data['payment_id'])
        self.age(first, minutes=4)
        self.assertEqual(self.start(link).status_code, 409)
        self.age(first, minutes=6)
        self.assertEqual(self.start(link).status_code, 200)

    def test_the_customer_who_opened_it_yesterday_pays_today_and_closes_the_invoice(self):
        link = self.create_link(hours_old=26)
        yesterday = self.payment(link, status=PaymentLinkPayment.STATUS_PENDING, gateway_transaction_id='',
                                 gateway_confirmation_code='', card_last4='', card_type='')
        self.age(yesterday, hours=25)
        issue_overdue_business_invoices()
        invoice = self.tax_invoices().get()

        started = self.start(link)
        self.assertEqual(started.status_code, 200, started.data)
        row = PaymentLinkPayment.objects.get(pk=started.data['payment_id'])
        self.assertEqual(self.approved_notify(row, index='901').status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.status, PaymentLinkPayment.STATUS_COMPLETED, row.review_reason)
        self.assertEqual(row.formal_document.document_type, 'receipt')
        self.assertEqual(balance_of(invoice).open, Decimal('0.00'))
        yesterday.refresh_from_db()
        self.assertEqual((yesterday.status, yesterday.failure_reason), ('failed', 'abandoned'))

    def test_a_late_approved_notify_for_an_abandoned_attempt_is_still_a_payment(self):
        link = self.create_link()
        first = PaymentLinkPayment.objects.get(pk=self.start(link).data['payment_id'])
        self.age(first, minutes=21)
        second = PaymentLinkPayment.objects.get(pk=self.start(link).data['payment_id'])
        first.refresh_from_db()
        self.assertEqual(first.failure_reason, 'abandoned')

        # The customer finished on the page opened first, after all.
        self.assertEqual(self.approved_notify(first, index='501').status_code, 200)
        first.refresh_from_db()
        self.assertEqual(first.status, PaymentLinkPayment.STATUS_COMPLETED, first.review_reason)
        self.assertEqual(first.failure_reason, '')
        self.assertEqual(first.gateway_transaction_id, '501')
        self.assertIsNotNone(first.paid_at)
        self.assertTrue(TranzilaTransaction.objects.filter(transaction_id='501', is_successful=True).exists())
        self.assertEqual(first.formal_document.document_type, 'combined')
        link.refresh_from_db()
        self.assertFalse(link.is_open())

        # And then on the second page too: recorded as paid, shown to the
        # office with the reason, and no second tax document.
        self.assertEqual(self.approved_notify(second, index='502').status_code, 200)
        second.refresh_from_db()
        self.assertEqual(second.status, PaymentLinkPayment.STATUS_COMPLETED, second.review_reason)
        self.assertIsNone(second.formal_document)
        self.assertIn(first.formal_document.document_number, second.document_error)
        self.assertEqual(FormalDocument.objects.count(), 1)
        self.assertEqual(TranzilaTransaction.objects.filter(is_successful=True).count(), 2)

    def test_a_late_notify_tranzila_does_not_confirm_goes_to_review_not_lost(self):
        link = self.create_link()
        first = PaymentLinkPayment.objects.get(pk=self.start(link).data['payment_id'])
        self.age(first, minutes=21)
        self.start(link)
        with patch(
            'apps.core.tranzila_service.TranzilaService.find_transaction',
            return_value={'success': True, 'transaction': None},
        ):
            response = self.public.post(CALLBACK, {
                'Response': '000', 'sum': '118.00', 'index': '601', 'ConfirmationCode': '0000123',
                'ccno': '4580', 'cardtype': '2', 'pdesc': str(first.id).replace('-', ''),
            })
        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        self.assertEqual(first.status, PaymentLinkPayment.STATUS_REVIEW)
        self.assertEqual(first.review_reason, 'unverified_callback')
        self.assertEqual(first.failure_reason, '')
        link.refresh_from_db()
        self.assertFalse(link.is_open())


class PublicPageFieldsTests(_Base):
    def test_a_business_page_names_its_customer_and_its_invoice(self):
        link = self.create_link(hours_old=25)
        url = f'/api/v1/payment-links/public/{link.slug}/'
        before = self.public.get(url)
        self.assertEqual(before.status_code, 200, before.content)
        self.assertEqual(set(before.data), {
            'slug', 'title', 'description', 'kind', 'options', 'payer_details_locked',
            'customer_name', 'invoice_number',
        })
        self.assertEqual(before.data['kind'], 'business_charge')
        self.assertEqual(before.data['customer_name'], self.customer.full_name)
        self.assertEqual(before.data['invoice_number'], '')
        # Nothing else about the customer reaches the page.
        text = before.content.decode()
        for private in (self.customer.phone, self.customer.email, self.customer.company_number):
            self.assertNotIn(private, text)

        issue_overdue_business_invoices()
        after = self.public.get(url)
        self.assertEqual(after.data['invoice_number'], self.tax_invoices().get().document_number)

    def test_a_general_page_says_its_kind_and_nothing_about_a_customer(self):
        general = PaymentLink.objects.create(title='מופע סוף שנה', business=self.business, created_by=self.manager)
        PaymentLinkOption.objects.create(link=general, label='כרטיס', amount=Decimal('50'))
        response = self.public.get(f'/api/v1/payment-links/public/{general.slug}/')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(set(response.data), {
            'slug', 'title', 'description', 'kind', 'options', 'payer_details_locked',
        })
        self.assertEqual(response.data['kind'], 'general')


@override_settings(CRON_TOKEN='cron-test-token')
class CronEndpointTests(_Base):
    def test_without_the_cron_token_it_answers_401_and_issues_nothing(self):
        self.create_link(hours_old=25)
        with patch.dict('os.environ', {'CRON_SECRET': ''}):
            self.assertEqual(self.public.get(CRON).status_code, 401)
            self.assertEqual(self.public.post(CRON, HTTP_X_CRON_TOKEN='wrong').status_code, 401)
            # A manager's session is not the cron either.
            self.assertEqual(self.client.get(CRON).status_code, 401)
        self.assertFalse(self.tax_invoices().exists())

    def test_with_the_token_it_runs_and_returns_the_summary(self):
        link = self.create_link(hours_old=25)
        self.create_link(hours_old=2)
        response = self.public.get(CRON, HTTP_AUTHORIZATION='Bearer cron-test-token')
        self.assertEqual(response.status_code, 200, response.content)
        invoice = self.tax_invoices().get()
        self.assertTrue(response.data['ok'])
        self.assertEqual(response.data['summary'], {
            'checked': 1, 'skipped': 0, 'errors': [],
            'issued': [{'link_id': str(link.id), 'document_number': invoice.document_number, 'total': '118.00'}],
        })
        again = self.public.post(CRON, HTTP_X_CRON_TOKEN='cron-test-token')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data['summary']['issued'], [])
        self.assertEqual(self.tax_invoices().count(), 1)
