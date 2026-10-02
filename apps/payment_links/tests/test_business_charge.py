from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Business, BusinessCategory, UserProfile
from apps.customers.models import BusinessCustomer
from apps.documents.models import DocumentSettlement, FormalDocument
from apps.documents.numbering import israel_today
from apps.documents.settlement import balance_of
from apps.payment_links.business_charge import issue_business_charge_document
from apps.payment_links.models import PaymentLink, PaymentLinkPayment

CREATE = '/api/v1/payment-links/links/business-charge/'


def user(role, email):
    row = get_user_model().objects.create_user(username=email, email=email, password='x')
    UserProfile.objects.update_or_create(user=row, defaults={'role': role})
    return row


@override_settings(TRANZILA_BILLING_TERMINAL='', DOCUMENT_SIGNING_ENABLED=False)
class BusinessChargeTests(TestCase):
    def setUp(self):
        self.manager = user(UserProfile.ROLE_MANAGER, 'business-charge@test.local')
        self.worker = user(UserProfile.ROLE_WORKER, 'business-charge-worker@test.local')
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

    def payload(self, **extra):
        data = {
            'business_customer_id': str(self.customer.id),
            'business_id': str(self.business.id),
            'business_category_id': str(self.category.id),
            'branch_id': str(self.branch.id),
            'amount': '118.00',
            'description': 'סדנה חד־פעמית',
        }
        data.update(extra)
        return data

    def create_link(self, **extra):
        response = self.client.post(CREATE, self.payload(**extra), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return PaymentLink.objects.get(pk=response.data['id'])

    def completed_payment(self, link):
        return PaymentLinkPayment.objects.create(
            link=link, option=link.options.get(), option_label='סדנה', amount=link.options.get().amount,
            payer_name=self.customer.full_name, payer_phone=self.customer.phone, payer_email=self.customer.email,
            status=PaymentLinkPayment.STATUS_COMPLETED, gateway_transaction_id='77',
            gateway_confirmation_code='0001234', card_last4='4580', card_type='Visa',
        )

    def invoice(self, kind, number):
        return FormalDocument.objects.create(
            document_number=number, document_type=kind, client_type='business',
            business_customer=self.customer, business=self.business, business_category=self.category,
            branch=self.branch, document_date=israel_today(), subtotal=Decimal('100'),
            vat_amount=Decimal('18'), total_amount=Decimal('118'),
        )

    def test_manager_creates_a_single_use_business_link(self):
        link = self.create_link()
        self.assertEqual(link.kind, PaymentLink.KIND_BUSINESS_CHARGE)
        self.assertEqual(link.business_customer, self.customer)
        self.assertEqual(link.options.get().amount, Decimal('118.00'))
        self.assertTrue(link.is_open())
        row = self.completed_payment(link)
        self.assertFalse(link.is_open())
        row.delete()
        self.assertTrue(link.is_open())

    def test_non_manager_cannot_create_a_charge(self):
        other = APIClient()
        other.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=self.worker).key}')
        self.assertEqual(other.post(CREATE, self.payload(), format='json').status_code, 403)

    def test_category_and_invoice_customer_are_checked(self):
        other_business = Business.objects.create(name='אחר')
        other_category = BusinessCategory.objects.create(business=other_business, name='אחרת')
        bad = self.client.post(CREATE, self.payload(business_category_id=str(other_category.id)), format='json')
        self.assertEqual(bad.status_code, 400)
        other_customer = BusinessCustomer.objects.create(first_name='לקוח', last_name='אחר')
        invoice = FormalDocument.objects.create(
            document_number='TI-X', document_type='tax_invoice', client_type='business',
            business_customer=other_customer, document_date=israel_today(), total_amount=Decimal('118'),
        )
        mismatch = self.client.post(CREATE, self.payload(target_invoice_id=str(invoice.id)), format='json')
        self.assertEqual(mismatch.status_code, 400)

    def test_new_sale_issues_one_invoice_receipt_with_card_details(self):
        payment = self.completed_payment(self.create_link())
        document = issue_business_charge_document(payment.id)
        payment.refresh_from_db()
        self.assertEqual(document.document_type, 'combined')
        self.assertEqual(document.total_amount, Decimal('118.00'))
        self.assertEqual((document.business, document.business_category), (self.business, self.category))
        self.assertEqual(document.payments.get().card_last_four, '4580')
        self.assertEqual(payment.formal_document, document)
        self.assertEqual(issue_business_charge_document(payment.id).id, document.id)
        self.assertEqual(FormalDocument.objects.filter(document_type='combined').count(), 1)

    def test_the_document_names_the_card_not_tranzilas_code(self):
        """The notify carries cardtype '2'; IRM-2026-000001 printed "סוג כרטיס: 2" (1.10.2026)."""
        from apps.payment_links.business_charge import card_brand_name

        payment = self.completed_payment(self.create_link())
        PaymentLinkPayment.objects.filter(pk=payment.pk).update(card_type='2')
        document = issue_business_charge_document(payment.id)
        self.assertEqual(document.payments.get().card_brand, 'ויזה')
        self.assertEqual(card_brand_name('1'), 'מאסטרקארד')
        self.assertEqual(card_brand_name('5'), 'ישראכרט')
        # A name, or a code Tranzila adds one day, is kept as it came.
        self.assertEqual(card_brand_name('Visa'), 'Visa')
        self.assertEqual(card_brand_name('9'), '9')
        self.assertEqual(card_brand_name(''), '')

    def test_tax_invoice_is_closed_by_a_receipt(self):
        invoice = self.invoice('tax_invoice', 'TI-TEST-1')
        payment = self.completed_payment(self.create_link(target_invoice_id=str(invoice.id)))
        document = issue_business_charge_document(payment.id)
        self.assertEqual(document.document_type, 'receipt')
        self.assertEqual(balance_of(invoice).open, Decimal('0.00'))
        self.assertEqual(DocumentSettlement.objects.get().payer, document)

    def test_transaction_invoice_is_closed_by_an_invoice_receipt(self):
        invoice = self.invoice('transaction_invoice', 'TX-TEST-1')
        payment = self.completed_payment(self.create_link(target_invoice_id=str(invoice.id)))
        document = issue_business_charge_document(payment.id)
        self.assertEqual(document.document_type, 'combined')
        self.assertEqual(balance_of(invoice).open, Decimal('0.00'))

    def test_the_invoice_is_named_once_in_the_title_and_on_the_document(self):
        """The screen fills "תשלום עבור חשבונית עסקה TX-…" by itself; it read twice."""
        invoice = self.invoice('transaction_invoice', 'TX-TEST-2')
        link = self.create_link(
            target_invoice_id=str(invoice.id), description='תשלום עבור חשבונית עסקה TX-TEST-2',
        )
        self.assertEqual(link.title, 'תשלום עבור חשבונית עסקה TX-TEST-2')
        document = issue_business_charge_document(self.completed_payment(link).id)
        line = document.line_items.get().description
        self.assertEqual(line.count('TX-TEST-2'), 1, line)

    @override_settings(
        CRM_API_BASE_URL='https://api.example.test',
        BUSINESS_CHARGE_ENABLED=True,
    )
    def test_business_charge_refuses_any_hosted_terminal_other_than_cogolive(self):
        link = self.create_link()
        public = APIClient()
        with patch('apps.payment_links.public_views.TranzilaService.iframe') as iframe:
            iframe.return_value.terminal = 'some-other-terminal'
            response = public.post(
                f'/api/v1/payment-links/public/{link.slug}/start/',
                {'option_id': str(link.options.get().id)}, format='json',
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn('Cogolive', response.data['error'])
        self.assertFalse(PaymentLinkPayment.objects.filter(link=link).exists())

    @override_settings(
        CRM_API_BASE_URL='https://api.example.test',
        BUSINESS_CHARGE_ENABLED=True,
    )
    def test_business_charge_uses_locked_customer_details_on_cogolive(self):
        link = self.create_link()
        public = APIClient()
        with patch('apps.payment_links.public_views.TranzilaService.iframe') as iframe:
            iframe.return_value.terminal = 'cogolive'
            iframe.return_value.create_payment_request.return_value = 'https://direct.tranzila.com/cogolive/iframenew.php'
            response = public.post(
                f'/api/v1/payment-links/public/{link.slug}/start/',
                {'option_id': str(link.options.get().id), 'payer_name': 'מישהו אחר'}, format='json',
            )
        self.assertEqual(response.status_code, 200, response.data)
        row = PaymentLinkPayment.objects.get(link=link)
        self.assertEqual((row.payer_name, row.payer_phone, row.payer_email), (
            self.customer.full_name, self.customer.phone, self.customer.email,
        ))
        self.assertIn('/cogolive/', response.data['iframe_url'])
        self.assertEqual(iframe.return_value.create_payment_request.call_args.kwargs['customer_name'], self.customer.full_name)

    @override_settings(
        CRM_API_BASE_URL='https://api.example.test',
        BUSINESS_CHARGE_ENABLED=True,
        TRANZILA_TERMINAL='general-hosted',
        BUSINESS_CHARGE_TRANZILA_TERMINAL='cogolive',
        TRANZILA_PUBLIC_KEY='pk-business-charge',
        TRANZILA_SECRET_KEY='sk-business-charge',
    )
    def test_the_payment_is_confirmed_on_the_terminal_it_was_taken_on(self):
        """
        The page opens on cogolive while the general hosted terminal is another
        one. The notify must be checked against cogolive's report — against the
        general terminal it was never found, and every business payment sat in
        review with no document (30.9.2026).
        """
        from zoneinfo import ZoneInfo

        from django.utils import timezone

        link = self.create_link()
        public = APIClient()
        with patch(
            'apps.core.tranzila_service.TranzilaService.create_payment_request',
            return_value='https://direct.tranzila.com/cogolive/iframenew.php',
        ):
            start = public.post(
                f'/api/v1/payment-links/public/{link.slug}/start/',
                {'option_id': str(link.options.get().id)}, format='json',
            )
        self.assertEqual(start.status_code, 200, start.data)
        row = PaymentLinkPayment.objects.get(link=link)
        self.assertEqual(row.tranzila_terminal, 'cogolive')

        asked = []

        def report(service, index):
            asked.append(service.terminal)
            local = timezone.now().astimezone(ZoneInfo('Asia/Jerusalem'))
            return {'success': True, 'transaction': {
                'index': '555', 'amount': '11800', 'processor_response_code': '000', 'tranmode': 'A',
                'authorization_number': '0000123',
                'transaction_date': local.strftime('%Y-%m-%d'), 'transaction_time': local.strftime('%H:%M:%S'),
            }}

        with patch('apps.core.tranzila_service.TranzilaService.find_transaction', autospec=True, side_effect=report), \
                self.captureOnCommitCallbacks(execute=True):
            res = public.post('/api/v1/payment-links/public/callback/', {
                'Response': '000', 'sum': '118.00', 'index': '555', 'ConfirmationCode': '0000123',
                'ccno': '4580', 'cardtype': '2', 'pdesc': str(row.id).replace('-', ''),
            })
        self.assertEqual(res.status_code, 200, res.content)
        row.refresh_from_db()
        self.assertEqual(asked, ['cogolive'])
        self.assertEqual(row.status, PaymentLinkPayment.STATUS_COMPLETED, row.review_reason)
        self.assertEqual(row.tranzila_terminal, 'cogolive')
        self.assertEqual(row.formal_document.document_type, 'combined')

    @override_settings(
        CRM_API_BASE_URL='https://api.example.test',
        TRANZILA_HOSTED_PAGE_ENABLED=True,
        BUSINESS_CHARGE_ENABLED=False,
    )
    def test_the_general_hosted_page_switch_does_not_open_a_business_charge(self):
        """Owner, 30.9.2026: business charges have a switch of their own."""
        link = self.create_link()
        with patch('apps.payment_links.public_views.TranzilaService.iframe') as iframe:
            iframe.return_value.terminal = 'cogolive'
            response = APIClient().post(
                f'/api/v1/payment-links/public/{link.slug}/start/',
                {'option_id': str(link.options.get().id)}, format='json',
            )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(PaymentLinkPayment.objects.filter(link=link).exists())

    @override_settings(
        CRM_API_BASE_URL='https://api.example.test',
        TRANZILA_HOSTED_PAGE_ENABLED=False,
        BUSINESS_CHARGE_ENABLED=True,
    )
    def test_the_business_switch_opens_business_charges_and_nothing_else(self):
        from apps.payment_links.models import PaymentLinkOption

        link = self.create_link()
        general = PaymentLink.objects.create(title='קישור כללי', created_by=self.manager)
        option = PaymentLinkOption.objects.create(link=general, label='כרטיס', amount=Decimal('50'))
        public = APIClient()
        with patch('apps.payment_links.public_views.TranzilaService.iframe') as iframe:
            iframe.return_value.terminal = 'cogolive'
            iframe.return_value.create_payment_request.return_value = 'https://direct.tranzila.com/cogolive/iframenew.php'
            business = public.post(
                f'/api/v1/payment-links/public/{link.slug}/start/',
                {'option_id': str(link.options.get().id)}, format='json',
            )
            other = public.post(
                f'/api/v1/payment-links/public/{general.slug}/start/',
                {'option_id': str(option.id), 'payer_name': 'דנה', 'payer_phone': '0501234567'}, format='json',
            )
        self.assertEqual(business.status_code, 200, business.data)
        self.assertTrue(iframe.return_value.create_payment_request.call_args.kwargs['hosted_page_allowed'])
        self.assertEqual(other.status_code, 503)

    @override_settings(TRANZILA_HOSTED_PAGE_ENABLED=False)
    def test_the_gateway_refuses_the_hosted_page_unless_the_flow_is_allowed(self):
        from apps.core.tranzila_service import HostedPageDisabled, TranzilaService

        with self.assertRaises(HostedPageDisabled):
            TranzilaService(terminal='cogolive').create_payment_request(amount=Decimal('10'))
        with self.assertRaises(HostedPageDisabled):
            TranzilaService(terminal='cogolive').create_payment_request(amount=Decimal('10'), hosted_page_allowed=False)
