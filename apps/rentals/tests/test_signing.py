"""Phase 3: the office sends a contract's signing link, the tenant opens it, reads the frozen contract and signs."""
import base64
import hashlib
import io
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import override_settings
from django.utils import timezone
from PIL import Image
from pypdf import PdfReader
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework.throttling import ScopedRateThrottle

from apps.core.models import UserProfile
from apps.customers.models import BusinessCustomer
from apps.rentals import signing
from apps.rentals.contracts import ContractError, issue_contract, void_contract
from apps.rentals.models import FrozenContractError, RentalContract, Tenancy
from apps.rentals.public_views import SigningPageView, SigningPdfView
from apps.rentals.tests.factories import (
    make_branch, make_customer, make_rental, make_studio, make_tenancy, make_user, sign_directly,
)
from apps.scheduling.rental_agreement import generator
from apps.scheduling.rental_agreement.text import contract_html, contract_paragraphs
from apps.signatures.models import Signature
from apps.signatures.tests.helpers import png_bytes, png_data_url
from apps.signatures.text import html_to_paragraphs

FRONTEND = 'https://crm.example.com'
CONTRACTS = '/api/v1/rentals/contracts/'
TENANCIES = '/api/v1/rentals/tenancies/'
SIGN = '/api/v1/rentals/sign/'

STALE = 'ההסכם השתנה אחרי שהחוזה הופק — הפיקו גרסה חדשה'
UPDATED = 'החוזה עודכן — בקשו מהמשרד קישור חדש'
WITHDRAWN = 'הקישור בוטל — בקשו מהמשרד קישור חדש'
EXPIRED = 'פג תוקף הקישור — בקשו מהמשרד קישור חדש'

PAGE_KEYS = {
    'state', 'version', 'tenant', 'branch_name', 'studio', 'slots', 'monthly_amount', 'vat_rate',
    'vat_amount', 'monthly_total', 'billing_day', 'start_date', 'end_date', 'document', 'signed_at',
    'signer_name', 'expires_at', 'pdf_url',
}
SLOT_KEYS = {
    'kind', 'weekday', 'date', 'day_label', 'start_time', 'end_time', 'branch_name', 'studio', 'rate', 'sum',
}


def link_url(contract_id):
    return f'{CONTRACTS}{contract_id}/signing-link/'


def cancel_url(contract_id):
    return f'{CONTRACTS}{contract_id}/signing-link/cancel/'


def signed_pdf_url(contract_id):
    return f'{CONTRACTS}{contract_id}/signed-pdf/'


def page_url(token):
    return f'{SIGN}{token}/'


def page_pdf_url(token):
    return f'{SIGN}{token}/pdf/'


def blank_png_data_url(color=(255, 255, 255, 0)):
    buffer = io.BytesIO()
    Image.new('RGBA', (300, 100), color).save(buffer, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


def submission(**overrides):
    return {
        'signer_name': '  אור   כהן ',
        'signer_id_number': '12345678-2',
        'signature': png_data_url(4),
        'accept': True,
        **overrides,
    }


@override_settings(CRM_FRONTEND_URL=FRONTEND)
class SigningTestCase(APITestCase):
    def setUp(self):
        # The public endpoints are throttled per client address, and the
        # counter lives in the cache: every test starts from zero.
        cache.clear()
        self.branch = make_branch('פלורנטין')
        self.manager = make_user('manager-signing@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)
        self.tenancy = make_tenancy(self.branch)
        self.slot = make_rental(
            self.branch, price='100', days=(0, 2), studio=make_studio(self.branch), tenancy=self.tenancy,
        )
        self.contract = issue_contract(self.tenancy, self.manager)
        self.public = APIClient()

    def fresh(self, contract=None):
        return RentalContract.objects.get(pk=(contract or self.contract).pk)

    def send_link(self, contract=None):
        res = self.client.post(link_url((contract or self.contract).pk), {}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        return res.data

    def token(self, contract=None):
        return self.fresh(contract).sign_token

    def sign(self, token=None, **overrides):
        return self.public.post(
            page_url(token or self.token()), submission(**overrides), format='json',
            HTTP_USER_AGENT='Mozilla/5.0 (Tenant phone)', HTTP_X_FORWARDED_FOR='203.0.113.7, 10.0.0.1',
        )

    def age_link(self, by):
        """Move the link's start back in time. A bulk update: the link fields of an open contract may move."""
        RentalContract.objects.filter(pk=self.contract.pk).update(
            sign_token_created_at=timezone.now() - by,
        )


class LinkLifecycleTests(SigningTestCase):
    def test_sends_a_link(self):
        before = timezone.now()
        data = self.send_link()
        contract = self.fresh()
        self.assertEqual(contract.status, 'sent')
        self.assertEqual(len(contract.sign_token), 10)
        self.assertTrue(contract.sign_token.isalnum())
        self.assertGreaterEqual(contract.sent_at, before)
        self.assertEqual(contract.sign_token_created_at, contract.sent_at)
        self.assertEqual(data['status'], 'sent')
        self.assertEqual(data['status_label'], 'נשלח')
        self.assertEqual(data['signing_url'], f'{FRONTEND}/s/{contract.sign_token}')
        expires = timezone.datetime.fromisoformat(data['signing_expires_at'])
        self.assertEqual(expires, contract.sign_token_created_at + timedelta(days=14))
        self.assertIsNotNone(data['sent_at'])
        self.assertEqual(
            (data['viewed_at'], data['signed_at'], data['signer_name'], data['signature_id'], data['signed_pdf_url']),
            (None, None, '', None, None),
        )
        # The tenancy's contracts list shows the same link.
        listed = self.client.get(f'{TENANCIES}{self.tenancy.pk}/contracts/').data
        self.assertEqual(listed[0]['signing_url'], data['signing_url'])

    @override_settings(CRM_FRONTEND_URL='', CORS_ALLOWED_ORIGINS=['https://office.example.com'])
    def test_without_a_configured_frontend_the_link_takes_the_offices_origin(self):
        res = self.client.post(link_url(self.contract.pk), {}, format='json', HTTP_ORIGIN='https://office.example.com')
        self.assertEqual(res.data['signing_url'], f'https://office.example.com/s/{self.token()}')

    def test_a_new_link_retires_the_one_before(self):
        first = self.send_link()
        old_token = self.token()
        self.public.get(page_url(old_token))
        self.assertEqual(self.fresh().status, 'viewed')

        second = self.send_link()
        contract = self.fresh()
        self.assertNotEqual(contract.sign_token, old_token)
        self.assertNotEqual(second['signing_url'], first['signing_url'])
        # A new link is sent and has not been opened.
        self.assertEqual((contract.status, contract.viewed_at), ('sent', None))
        self.assertEqual(self.public.get(page_url(old_token)).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.public.get(page_url(contract.sign_token)).data['state'], 'open')

    def test_withdrawing_the_link(self):
        self.send_link()
        token = self.token()
        res = self.client.post(cancel_url(self.contract.pk), {}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(
            (res.data['status'], res.data['signing_url'], res.data['signing_expires_at'], res.data['sent_at']),
            ('draft', None, None, None),
        )
        contract = self.fresh()
        self.assertIsNone(contract.sign_token)
        self.assertIsNone(contract.sign_token_created_at)
        self.assertEqual(self.public.get(page_url(token)).status_code, status.HTTP_404_NOT_FOUND)

        again = self.client.post(cancel_url(self.contract.pk), {}, format='json')
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(again.data, {'error': 'לחוזה הזה אין קישור פעיל לביטול'})
        # And it can be sent again.
        self.send_link()
        self.assertEqual(self.fresh().status, 'sent')

    def test_the_link_lives_fourteen_days(self):
        self.send_link()
        token = self.token()
        self.age_link(timedelta(days=14) - timedelta(minutes=1))
        self.assertEqual(self.public.get(page_url(token)).data['state'], 'open')

        self.age_link(timedelta(days=14, seconds=1))
        page = self.public.get(page_url(token))
        self.assertEqual(page.status_code, status.HTTP_200_OK)
        self.assertEqual(page.data, {'state': 'expired', 'message': EXPIRED, 'version': 1})
        refused = self.sign(token)
        self.assertEqual(refused.status_code, status.HTTP_410_GONE)
        self.assertEqual(refused.data, {'error': EXPIRED, 'state': 'expired'})
        self.assertEqual(self.public.get(page_pdf_url(token)).status_code, status.HTTP_410_GONE)
        # The office no longer sees a live link, and can send a new one.
        listed = self.client.get(f'{TENANCIES}{self.tenancy.pk}/contracts/').data[0]
        # Opened on day 13, so 'viewed' — but no live link any more.
        self.assertEqual((listed['status'], listed['signing_url'], listed['signing_expires_at']), ('viewed', None, None))
        self.send_link()
        self.assertEqual(self.public.get(page_url(self.token())).data['state'], 'open')

    def test_refuses_a_contract_that_cannot_be_sent(self):
        self.send_link()
        # Stale: the agreement changed after the contract was issued.
        self.client.patch(f'{TENANCIES}{self.tenancy.pk}/', {'monthly_amount': '999.00'}, format='json')
        res = self.client.post(link_url(self.contract.pk), {}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data, {'error': STALE})

        # Void, and so not the current one: a new version replaced it.
        newer = issue_contract(self.tenancy, self.manager)
        res = self.client.post(link_url(self.contract.pk), {}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data, {'error': 'החוזה בוטל. יש להפיק גרסה חדשה ולשלוח אותה'})
        cancel = self.client.post(cancel_url(self.contract.pk), {}, format='json')
        self.assertEqual(cancel.status_code, status.HTTP_400_BAD_REQUEST)

        # Signed.
        sign_directly(newer)
        for url in (link_url(newer.pk), cancel_url(newer.pk)):
            with self.subTest(url=url):
                res = self.client.post(url, {}, format='json')
                self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsNone(self.fresh(newer).sign_token)

    def test_only_the_current_contract_gets_a_link(self):
        # A contract that is not void yet not the newest cannot be made through
        # the API; the service refuses it all the same.
        older = self.contract
        with mock.patch('apps.rentals.signing.current_contract', return_value=RentalContract(pk=None)):
            with self.assertRaises(ContractError) as refused:
                signing.issue_signing_link(older)
        self.assertEqual(refused.exception.message, 'זו אינה הגרסה העדכנית של החוזה. יש לשלוח את הגרסה העדכנית')


class PublicPageTests(SigningTestCase):
    def test_shows_the_frozen_terms_not_the_live_ones(self):
        self.send_link()
        token = self.token()
        terms = self.fresh().terms
        # The office edits the agreement and the tenant's card after sending.
        self.client.patch(f'{TENANCIES}{self.tenancy.pk}/', {'monthly_amount': '5000.00'}, format='json')
        BusinessCustomer.objects.filter(pk=self.tenancy.tenant_id).update(first_name='שם', last_name='חדש', phone='03-0000000')

        res = self.public.get(page_url(token))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        page = res.data
        self.assertEqual(set(page), PAGE_KEYS)
        self.assertEqual(page['state'], 'open')
        self.assertEqual(page['version'], 1)
        self.assertEqual(page['tenant'], {
            'name': 'סטודיו אור', 'id_number': '', 'company_number': '512345678',
            'phone': '050-1234567', 'email': 'or@example.com',
        })
        self.assertEqual(page['studio'], {
            'name': terms['studio']['name'], 'company_number': terms['studio']['company_number'],
            'phone': terms['studio']['phone'], 'email': terms['studio']['email'],
        })
        self.assertEqual(page['branch_name'], 'פלורנטין')
        self.assertEqual(
            (page['monthly_amount'], page['vat_rate'], page['vat_amount'], page['monthly_total']),
            ('1234.56', '0.18', '222.22', '1456.78'),
        )
        self.assertEqual((page['billing_day'], page['start_date'], page['end_date']), (10, '2026-09-01', '2027-08-31'))
        self.assertEqual(len(page['slots']), 2)
        self.assertEqual(set(page['slots'][0]), SLOT_KEYS)
        self.assertEqual(
            [(slot['day_label'], slot['start_time'], slot['rate'], slot['sum']) for slot in page['slots']],
            [('ראשון', '10:00', '100.00', '400.00'), ('שלישי', '10:00', '100.00', '400.00')],
        )
        self.assertEqual(page['document'], contract_paragraphs(terms))
        self.assertIn('שם המפעיל: סטודיו אור | ח.פ: 512345678', page['document'])
        self.assertEqual((page['signed_at'], page['signer_name']), (None, ''))
        self.assertEqual(page['pdf_url'], page_pdf_url(token))
        self.assertIsNotNone(page['expires_at'])

        # The page stays open on what was sent; the signing is what refuses it.
        refused = self.sign(token)
        self.assertEqual(refused.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(refused.data, {'error': UPDATED})
        self.assertFalse(Signature.objects.exists())
        self.assertEqual(self.fresh().status, 'viewed')

    def test_the_first_open_marks_it_viewed_once(self):
        self.send_link()
        token = self.token()
        self.public.get(page_url(token))
        first = self.fresh()
        self.assertEqual(first.status, 'viewed')
        self.assertIsNotNone(first.viewed_at)
        self.public.get(page_url(token))
        self.assertEqual(self.fresh().viewed_at, first.viewed_at)
        office = self.client.get(f'{TENANCIES}{self.tenancy.pk}/contracts/').data[0]
        self.assertEqual((office['status'], office['status_label']), ('viewed', 'נצפה'))
        self.assertIsNotNone(office['viewed_at'])

    def test_an_unknown_token_is_not_found(self):
        for token in ('AbCdEf1234', 'short', 'x' * 40, 'bad-token!'):
            with self.subTest(token=token):
                res = self.public.get(page_url(token))
                self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(res.data, {'error': signing.NOT_FOUND})
                self.assertEqual(self.public.post(page_url(token), submission(), format='json').status_code, 404)
                self.assertEqual(self.public.get(page_pdf_url(token)).status_code, 404)

    def test_a_replaced_or_voided_contract_is_cancelled(self):
        self.send_link()
        token = self.token()
        void_contract(self.contract, 'טעות')
        page = self.public.get(page_url(token)).data
        self.assertEqual(page, {'state': 'cancelled', 'message': WITHDRAWN, 'version': 1})
        refused = self.sign(token)
        self.assertEqual(refused.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(refused.data, {'error': WITHDRAWN, 'state': 'cancelled'})

        issue_contract(self.tenancy, self.manager)
        self.assertEqual(self.public.get(page_url(token)).data['message'], UPDATED)
        self.assertEqual(self.sign(token).data, {'error': UPDATED, 'state': 'cancelled'})
        self.assertEqual(self.public.get(page_pdf_url(token)).status_code, status.HTTP_410_GONE)
        self.assertFalse(Signature.objects.exists())

    def test_the_pdf_is_the_contract_as_issued(self):
        self.send_link()
        res = self.public.get(page_pdf_url(self.token()))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res['Content-Type'], 'application/pdf')
        self.assertEqual(res['Content-Disposition'], 'inline; filename="rental-contract-v1.pdf"')
        self.assertEqual(res.content, bytes(self.contract.pdf))

    def test_the_document_is_what_the_signature_keeps(self):
        terms = self.contract.terms
        paragraphs = contract_paragraphs(terms)
        self.assertEqual(html_to_paragraphs(contract_html(terms)), paragraphs)
        self.assertEqual(paragraphs[0], 'הסכם הרשאת שימוש בסטודיו קוגומלו')
        self.assertEqual(paragraphs[-1], '7. ולראיה באו הצדדים על החתום, בהסכמה מלאה:')
        self.assertIn('ראשון · 10:00-11:00 · פלורנטין · סטודיו 1 · תעריף שעתי (לפני מע"מ): ₪100.00 · '
                      'סה"כ לחודש (לפני מע"מ): ₪400.00', paragraphs)
        self.assertIn('סה"כ לתשלום חודשי (כולל מע"מ): ₪1456.78', paragraphs)


class SigningHappyPathTests(SigningTestCase):
    @override_settings(RESEND_API_KEY='re_test_key')
    def test_signs_the_contract(self):
        self.send_link()
        token = self.token()
        self.public.get(page_url(token))
        issued = self.fresh()

        # Every line the renderer draws passes through _rtl_line on its way into
        # the PDF: collect them to read the signed copy's signature block.
        # (pypdf cannot extract the Latin and digits of these Heebo subsets.)
        drawn = []
        original_rtl_line = generator._rtl_line

        def spy(text):
            drawn.append(text)
            return original_rtl_line(text)

        with mock.patch('apps.rentals.signed_email.send_resend_email') as send, \
                mock.patch.object(generator, '_rtl_line', spy):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                res = self.sign(token)
            # Queued for after the commit, not sent inside the transaction.
            self.assertEqual(len(callbacks), 1)
            send.assert_not_called()
            callbacks[0]()

        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        contract = self.fresh()
        signature = contract.signature
        self.assertEqual(res.data, {
            'state': 'signed', 'signed_at': contract.signed_at.isoformat(), 'pdf_url': page_pdf_url(token),
            'next': 'done',
        })

        # The contract: signed, with its signature and signed copy; the issued PDF as it was.
        self.assertEqual(contract.status, 'signed')
        self.assertEqual(contract.signed_at, signature.signed_at)
        self.assertEqual(bytes(contract.pdf), bytes(issued.pdf))
        self.assertEqual(contract.pdf_sha256, issued.pdf_sha256)
        self.assertTrue(bytes(contract.signed_pdf).startswith(b'%PDF'))
        self.assertNotEqual(bytes(contract.signed_pdf), bytes(contract.pdf))
        self.assertTrue(contract.signed_pdf_is_intact())
        self.assertEqual(contract.sign_token, token)
        self.assertEqual(Tenancy.objects.get(pk=self.tenancy.pk).status, 'signed')

        # The signature row: who, what exactly, from where.
        terms = contract.terms
        png = png_bytes(4)
        self.assertEqual(signature.kind, Signature.KIND_RENTAL_CONTRACT)
        self.assertEqual(signature.source, Signature.SOURCE_SIGNING_LINK)
        self.assertEqual(signature.business_customer_id, self.tenancy.tenant_id)
        self.assertEqual(signature.branch_id, self.branch.pk)
        self.assertIsNone(signature.family_id)
        self.assertEqual((signature.signer_name, signature.signer_id_number), ('אור כהן', '123456782'))
        self.assertEqual((signature.signer_phone, signature.signer_email), ('050-1234567', 'or@example.com'))
        self.assertEqual(signature.document_title, 'חוזה שכירות — גרסה 1')
        self.assertEqual(signature.document_html, contract_html(terms))
        self.assertEqual(html_to_paragraphs(signature.document_html), contract_paragraphs(terms))
        self.assertEqual(signature.document_sha256, hashlib.sha256(signature.document_html.encode('utf-8')).hexdigest())
        self.assertEqual(signature.consents, {'terms': True, 'computerized_documents': True})
        self.assertEqual(bytes(signature.signature_png), png)
        self.assertEqual(signature.signature_sha256, hashlib.sha256(png).hexdigest())
        self.assertEqual(signature.refs, {
            'contract_id': str(contract.pk), 'tenancy_id': str(self.tenancy.pk), 'version': 1,
            'terms_sha256': contract.terms_sha256, 'pdf_sha256': contract.pdf_sha256,
        })
        self.assertEqual((signature.ip_address, signature.user_agent), ('203.0.113.7', 'Mozilla/5.0 (Tenant phone)'))

        # The signed copy names the signer, the time in Israel, the exact terms and the signature.
        israel = contract.signed_at.astimezone(generator.ISRAEL_TZ)
        for line in (
            f'תאריך חתימה: {israel:%d/%m/%Y}',
            'אור כהן · ת.ז./ח.פ: 123456782',
            f'נחתם ב־{israel:%d/%m/%Y %H:%M} (שעון ישראל)',
            f'מזהה תנאים: {contract.terms_sha256}',
            f'מזהה חתימה: {signature.pk}',
        ):
            self.assertIn(line, drawn)
        self.assertEqual(len(PdfReader(io.BytesIO(bytes(contract.signed_pdf))).pages), len(PdfReader(io.BytesIO(bytes(contract.pdf))).pages))

        # The email: the signed copy, to the address the contract states.
        send.assert_called_once()
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs['to'], ['or@example.com'])
        self.assertEqual(kwargs['subject'], 'חוזה השכירות החתום — גרסה 1')
        (attachment,) = kwargs['attachments']
        self.assertEqual(attachment['filename'], 'rental-contract-v1-signed.pdf')
        self.assertEqual(base64.b64decode(attachment['content']), bytes(contract.signed_pdf))

    def test_the_signed_contract_on_every_screen(self):
        self.send_link()
        token = self.token()
        self.assertEqual(self.sign(token).status_code, status.HTTP_200_OK)
        contract = self.fresh()

        page = self.public.get(page_url(token)).data
        self.assertEqual(page['state'], 'signed')
        self.assertEqual((page['signer_name'], page['signed_at']), ('אור כהן', contract.signed_at.isoformat()))
        self.assertIsNone(page['expires_at'])
        pdf = self.public.get(page_pdf_url(token))
        self.assertEqual(pdf['Content-Disposition'], 'inline; filename="rental-contract-v1-signed.pdf"')
        self.assertEqual(pdf.content, bytes(contract.signed_pdf))

        office = self.client.get(f'{TENANCIES}{self.tenancy.pk}/contracts/').data[0]
        self.assertEqual(office['status'], 'signed')
        self.assertEqual(office['signer_name'], 'אור כהן')
        self.assertEqual(office['signature_id'], str(contract.signature_id))
        self.assertEqual(office['signed_pdf_url'], signed_pdf_url(contract.pk))
        self.assertIsNotNone(office['signed_at'])
        self.assertEqual((office['signing_url'], office['signing_expires_at']), (None, None))

        download = self.client.get(signed_pdf_url(contract.pk))
        self.assertEqual(download.status_code, status.HTTP_200_OK)
        self.assertEqual(download['Content-Disposition'], 'attachment; filename="rental-contract-v1-signed.pdf"')
        self.assertEqual(download.content, bytes(contract.signed_pdf))
        # The issued PDF stays the issued PDF.
        self.assertEqual(self.client.get(f'{CONTRACTS}{contract.pk}/pdf/').content, bytes(contract.pdf))

        # The office's signatures list names the merchant; the family is empty.
        listed = self.client.get('/api/v1/signatures/').data['results']
        self.assertEqual(
            (listed[0]['kind'], listed[0]['family_name'], listed[0]['business_customer_name'], listed[0]['branch_name']),
            ('rental_contract', None, 'סטודיו אור', 'פלורנטין'),
        )

    def test_a_signed_copy_that_changed_is_never_served(self):
        self.send_link()
        self.sign()
        with connection.cursor() as cursor:
            cursor.execute('UPDATE rental_contracts SET signed_pdf = %s WHERE id = %s', [b'%PDF-1.4 not it', self.contract.pk])
        with self.assertLogs('apps.rentals.views', 'ERROR'), self.assertLogs('django.request', 'ERROR'):
            res = self.client.get(signed_pdf_url(self.contract.pk))
        self.assertEqual(res.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        with self.assertLogs('apps.rentals.public_views', 'ERROR'), self.assertLogs('django.request', 'ERROR'):
            res = self.public.get(page_pdf_url(self.token()))
        self.assertEqual(res.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

    def test_an_unsigned_contract_has_no_signed_copy(self):
        res = self.client.get(signed_pdf_url(self.contract.pk))
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(res.data, {'error': 'לחוזה הזה אין עותק חתום'})

    @override_settings(RESEND_API_KEY='re_test_key')
    def test_a_failing_email_does_not_fail_the_signing(self):
        self.send_link()
        with mock.patch('apps.rentals.signed_email.send_resend_email', side_effect=RuntimeError('Resend is down')):
            with self.assertLogs('apps.rentals.signed_email', 'ERROR') as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    res = self.sign()
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.fresh().status, 'signed')
        self.assertIn(str(self.contract.pk), logs.output[0])

    @override_settings(RESEND_API_KEY='re_test_key')
    def test_no_email_on_the_contract_sends_nothing(self):
        tenant = self.tenancy.tenant
        BusinessCustomer.objects.filter(pk=tenant.pk).update(email='')
        contract = issue_contract(self.tenancy, self.manager)
        self.send_link(contract)
        with mock.patch('apps.rentals.signed_email.send_resend_email') as send:
            with self.captureOnCommitCallbacks(execute=True):
                res = self.sign(self.token(contract))
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        send.assert_not_called()

    def test_a_tenancy_past_signed_keeps_its_status(self):
        Tenancy.objects.filter(pk=self.tenancy.pk).update(status=Tenancy.STATUS_ACTIVE)
        self.send_link()
        self.assertEqual(self.sign().status_code, status.HTTP_200_OK)
        self.assertEqual(Tenancy.objects.get(pk=self.tenancy.pk).status, 'active')

    def test_a_company_number_signs_as_well(self):
        self.send_link()
        res = self.sign(signer_id_number='512345678')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(Signature.objects.get().signer_id_number, '512345678')

    def test_an_id_typed_without_its_leading_zero(self):
        self.send_link()
        self.assertEqual(self.sign(signer_id_number='12345674').status_code, status.HTTP_200_OK)
        self.assertEqual(Signature.objects.get().signer_id_number, '012345674')


class SigningRefusalTests(SigningTestCase):
    def setUp(self):
        super().setUp()
        self.send_link()

    def assert_nothing_signed(self):
        self.assertFalse(Signature.objects.exists())
        contract = self.fresh()
        self.assertNotEqual(contract.status, 'signed')
        self.assertIsNone(contract.signed_pdf)
        self.assertEqual(Tenancy.objects.get(pk=self.tenancy.pk).status, 'draft')

    def test_bad_input_is_refused_in_hebrew(self):
        cases = [
            ('no accept', {'accept': None}, 'יש לאשר את תנאי החוזה ואת קבלת המסמכים הממוחשבים לפני החתימה'),
            ('accept false', {'accept': False}, 'יש לאשר את תנאי החוזה ואת קבלת המסמכים הממוחשבים לפני החתימה'),
            ('accept as text', {'accept': 'true'}, 'יש לאשר את תנאי החוזה ואת קבלת המסמכים הממוחשבים לפני החתימה'),
            ('no name', {'signer_name': ' '}, 'יש להזין את השם המלא של החותם'),
            ('no id', {'signer_id_number': ''}, 'מספר תעודת הזהות או מספר החברה אינו תקין'),
            ('bad check digit', {'signer_id_number': '123456789'}, 'מספר תעודת הזהות או מספר החברה אינו תקין'),
            ('letters', {'signer_id_number': '12345678a'}, 'מספר תעודת הזהות או מספר החברה אינו תקין'),
            ('too short', {'signer_id_number': '18'}, 'מספר תעודת הזהות או מספר החברה אינו תקין'),
            ('no signature', {'signature': ''}, 'החתימה לא התקבלה. חתמו שוב בתוך המסגרת'),
            ('not a png', {'signature': 'data:image/png;base64,' + base64.b64encode(b'GIF89a...').decode()},
             'החתימה לא התקבלה. חתמו שוב בתוך המסגרת'),
            ('not base64', {'signature': 'data:image/png;base64,@@@'}, 'החתימה לא התקבלה. חתמו שוב בתוך המסגרת'),
            ('png magic, broken image', {'signature': 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\nxx').decode()},
             'החתימה לא התקבלה. חתמו שוב בתוך המסגרת'),
            ('empty pad', {'signature': blank_png_data_url()}, 'החתימה ריקה. חתמו באצבע בתוך המסגרת'),
            ('empty white pad', {'signature': blank_png_data_url((255, 255, 255, 255))}, 'החתימה ריקה. חתמו באצבע בתוך המסגרת'),
        ]
        for label, change, message in cases:
            with self.subTest(label):
                cache.clear()
                res = self.sign(**change)
                self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.data)
                self.assertEqual(res.data, {'error': message})
        self.assert_nothing_signed()

    def test_already_signed(self):
        self.assertEqual(self.sign().status_code, status.HTTP_200_OK)
        again = self.sign()
        self.assertEqual(again.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(again.data, {'error': 'החוזה כבר נחתם', 'state': 'signed'})
        self.assertEqual(Signature.objects.count(), 1)

    def test_stale(self):
        self.client.patch(f'{TENANCIES}{self.tenancy.pk}/', {'billing_day': 20}, format='json')
        res = self.sign()
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(res.data, {'error': UPDATED})
        self.assert_nothing_signed()

    def test_withdrawn(self):
        token = self.token()
        self.client.post(cancel_url(self.contract.pk), {}, format='json')
        self.assertEqual(self.sign(token).status_code, status.HTTP_404_NOT_FOUND)
        self.assert_nothing_signed()

    def test_a_link_rotated_while_the_page_was_open(self):
        token = self.token()
        opened = signing.resolve_sign_token(token)
        self.send_link()
        signing_input = signing.clean_signing_input(submission())
        with self.assertRaises(signing.SigningError) as refused:
            signing.sign_contract(opened, token, signing_input, mock.Mock(META={}))
        self.assertEqual((refused.exception.status_code, refused.exception.state), (410, 'cancelled'))
        self.assert_nothing_signed()

    def test_a_contract_voided_while_the_page_was_open(self):
        token = self.token()
        opened = signing.resolve_sign_token(token)
        signing_input = signing.clean_signing_input(submission())
        issue_contract(self.tenancy, self.manager)
        with self.assertRaises(signing.SigningError) as refused:
            signing.sign_contract(opened, token, signing_input, mock.Mock(META={}))
        self.assertEqual(
            (refused.exception.status_code, refused.exception.message, refused.exception.state),
            (409, UPDATED, 'cancelled'),
        )
        self.assert_nothing_signed()

    def test_expired_while_the_page_was_open(self):
        token = self.token()
        opened = signing.resolve_sign_token(token)
        signing_input = signing.clean_signing_input(submission())
        self.age_link(timedelta(days=15))
        with self.assertRaises(signing.SigningError) as refused:
            signing.sign_contract(opened, token, signing_input, mock.Mock(META={}))
        self.assertEqual((refused.exception.status_code, refused.exception.state), (410, 'expired'))
        self.assert_nothing_signed()

    def test_terms_changed_behind_the_applications_back_are_never_signed(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """UPDATE rental_contracts SET terms = jsonb_set(terms, '{monthly_amount}', '"1.00"') WHERE id = %s""",
                [self.contract.pk],
            )
        with self.assertLogs('apps.rentals.signing', 'ERROR'), self.assertLogs('django.request', 'ERROR'):
            res = self.sign()
        self.assertEqual(res.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assert_nothing_signed()


class ThrottleTests(SigningTestCase):
    def test_the_rates_are_configured(self):
        rates = ScopedRateThrottle.THROTTLE_RATES
        self.assertEqual((rates['rental_sign_view'], rates['rental_sign_submit']), ('30/min', '5/min'))
        for view in (SigningPageView, SigningPdfView):
            self.assertEqual(view.throttle_classes, [ScopedRateThrottle])
            self.assertEqual(view.authentication_classes, [])

    def test_reading_and_signing_have_their_own_limits(self):
        self.send_link()
        token = self.token()
        with mock.patch.object(ScopedRateThrottle, 'THROTTLE_RATES', {'rental_sign_view': '2/min', 'rental_sign_submit': '1/min'}):
            self.assertEqual(self.public.get(page_url(token)).status_code, 200)
            self.assertEqual(self.public.get(page_pdf_url(token)).status_code, 200)
            self.assertEqual(self.public.get(page_url(token)).status_code, status.HTTP_429_TOO_MANY_REQUESTS)
            self.assertEqual(self.sign(token, accept=False).status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(self.sign(token).status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class OfficeScopingTests(SigningTestCase):
    """The office's signing endpoints are the contracts' own: a partner's branches only, and no worker."""

    def setUp(self):
        super().setUp()
        self.theirs = make_branch('רמת אביב')
        their_tenancy = make_tenancy(self.theirs, tenant=make_customer('יעל', 'בר', branch=self.theirs))
        make_rental(self.theirs, tenancy=their_tenancy)
        self.their_contract = issue_contract(their_tenancy, self.manager)
        sign_directly(self.their_contract)
        self.partner = make_user('partner-signing@test', UserProfile.ROLE_PARTNER, branches=[self.branch])

    def reach(self, contract_id):
        return [
            self.client.post(link_url(contract_id), {}, format='json'),
            self.client.post(cancel_url(contract_id), {}, format='json'),
            self.client.get(signed_pdf_url(contract_id)),
        ]

    def test_another_branchs_partner_finds_nothing(self):
        self.client.force_authenticate(self.partner)
        for res in self.reach(self.their_contract.pk):
            self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_partner_without_branches_finds_nothing(self):
        self.client.force_authenticate(make_user('partner-nobranch-signing@test', UserProfile.ROLE_PARTNER))
        for res in self.reach(self.contract.pk):
            self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.fresh().status, 'draft')

    def test_a_worker_is_refused(self):
        self.client.force_authenticate(make_user('worker-signing@test', UserProfile.ROLE_WORKER, branches=[self.branch]))
        for res in self.reach(self.contract.pk):
            self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.fresh().status, 'draft')

    def test_a_partner_sends_their_own_branchs_contract(self):
        self.client.force_authenticate(self.partner)
        sent = self.client.post(link_url(self.contract.pk), {}, format='json')
        self.assertEqual(sent.status_code, status.HTTP_200_OK, sent.data)
        self.assertTrue(sent.data['signing_url'].startswith(f'{FRONTEND}/s/'))
        self.assertEqual(self.client.post(cancel_url(self.contract.pk), {}, format='json').status_code, 200)


class SignedContractIsFinalTests(SigningTestCase):
    """Once signed, nothing changes: no void, no link, no field — by the API, the model or a bulk update."""

    def setUp(self):
        super().setUp()
        self.send_link()
        self.assertEqual(self.sign().status_code, status.HTTP_200_OK)
        self.signed = self.fresh()

    def test_the_office_can_neither_void_nor_relink_it(self):
        for res, message in (
            (self.client.post(f'{CONTRACTS}{self.signed.pk}/void/', {'reason': 'x'}, format='json'), 'אי אפשר לבטל חוזה חתום'),
            (self.client.post(link_url(self.signed.pk), {}, format='json'), 'החוזה כבר נחתם, ולכן אין לו קישור חדש'),
            (self.client.post(cancel_url(self.signed.pk), {}, format='json'), 'אי אפשר לבטל את הקישור של חוזה חתום'),
            (self.client.post(f'{TENANCIES}{self.tenancy.pk}/contracts/', {}, format='json'), 'יש כבר חוזה חתום להסכם הזה'),
        ):
            with self.subTest(message=message):
                self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(res.data, {'error': message})
        after = self.fresh()
        self.assertEqual((after.status, after.sign_token, after.signature_id), ('signed', self.signed.sign_token, self.signed.signature_id))

    def test_no_field_changes_through_the_model(self):
        other = Signature.objects.create(
            kind=Signature.KIND_RENTAL_CONTRACT, signed_at=timezone.now(), signer_name='x', document_title='x',
            document_html='x', document_sha256='0' * 64, signature_png=b'x', signature_sha256='0' * 64,
        )
        changes = {
            'status': 'void', 'voided_at': timezone.now(), 'void_reason': 'x', 'sign_token': 'NewToken01',
            'sign_token_created_at': timezone.now(), 'sent_at': timezone.now(), 'viewed_at': timezone.now(),
            'signed_at': timezone.now(), 'signature': other, 'signed_pdf': b'%PDF-other', 'signed_pdf_sha256': '0' * 64,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                contract = self.fresh()
                setattr(contract, field, value)
                with self.assertRaises(FrozenContractError):
                    contract.save()
        self.assertEqual(bytes(self.fresh().signed_pdf), bytes(self.signed.signed_pdf))

    def test_no_bulk_update_touches_it(self):
        same = RentalContract.objects.filter(pk=self.signed.pk)
        for change in ({'void_reason': 'x'}, {'status': 'void'}, {'sign_token': None}, {'viewed_at': None}):
            with self.subTest(change=list(change)):
                with self.assertRaises(FrozenContractError):
                    same.update(**change)

    def test_the_signature_cannot_be_deleted_from_under_it(self):
        with self.assertRaises(ProtectedError):
            self.signed.signature.delete()

    def test_deleting_the_issuers_account_still_works(self):
        self.manager.delete()
        stored = self.fresh()
        self.assertIsNone(stored.created_by_id)
        self.assertEqual(stored.status, 'signed')


class SigningFieldsRuleTests(SigningTestCase):
    """What signing writes is written only by the save that signs; a void contract never reopens."""

    def test_the_signing_fields_are_not_set_on_their_own(self):
        contract = self.fresh()
        contract.signed_at = timezone.now()
        with self.assertRaises(FrozenContractError):
            contract.save()
        same = RentalContract.objects.filter(pk=self.contract.pk)
        for change in ({'signed_at': timezone.now()}, {'signed_pdf': b'%PDF'}, {'signed_pdf_sha256': '0' * 64},
                       {'signature': None}, {'status': 'signed'}):
            with self.subTest(change=list(change)):
                with self.assertRaises(FrozenContractError):
                    same.update(**change)

    def test_the_database_refuses_a_signed_contract_without_its_signature(self):
        contract = self.fresh()
        contract.status = RentalContract.STATUS_SIGNED
        with self.assertRaises(IntegrityError), transaction.atomic():
            contract.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('UPDATE rental_contracts SET sign_token = %s WHERE id = %s', ['Tok1234567', self.contract.pk])

    def test_a_void_contract_never_reopens_nor_gets_a_link(self):
        void_contract(self.contract, 'x')
        for field, value in (('status', 'draft'), ('sign_token', 'NewToken01'), ('sent_at', timezone.now())):
            with self.subTest(field=field):
                contract = self.fresh()
                setattr(contract, field, value)
                if field == 'sign_token':
                    contract.sign_token_created_at = timezone.now()
                with self.assertRaises(FrozenContractError):
                    contract.save()
        with self.assertRaises(FrozenContractError):
            RentalContract.objects.filter(pk=self.contract.pk).update(status='draft')
        # Its reason may still be corrected, as in phase 2.
        voided = self.fresh()
        voided.void_reason = 'טעות בשם'
        voided.save(update_fields=['void_reason'])
        self.assertEqual(self.fresh().void_reason, 'טעות בשם')


class OfficeViewAndCachingTests(SigningTestCase):
    """The office checking a link, the tenant's phone caching the PDF, and the tenants row's summary."""

    def test_an_office_user_opening_the_link_does_not_mark_it_viewed(self):
        from rest_framework.authtoken.models import Token

        self.send_link()
        staff = APIClient()
        staff.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=self.manager).key}')
        self.assertEqual(staff.get(page_url(self.token())).status_code, status.HTTP_200_OK)
        self.assertIsNone(self.fresh().viewed_at)

        self.assertEqual(self.public.get(page_url(self.token())).status_code, status.HTTP_200_OK)
        self.assertIsNotNone(self.fresh().viewed_at)

    def test_a_token_that_is_not_real_counts_as_the_tenant(self):
        self.send_link()
        stranger = APIClient()
        stranger.credentials(HTTP_AUTHORIZATION='Token not-a-real-token')
        self.assertEqual(stranger.get(page_url(self.token())).status_code, status.HTTP_200_OK)
        self.assertIsNotNone(self.fresh().viewed_at)

    def test_the_public_pdf_is_never_cached(self):
        self.send_link()
        res = self.public.get(page_pdf_url(self.token()))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res['Cache-Control'], 'no-store')

    def test_the_tenants_row_shows_where_the_link_stands(self):
        self.send_link()
        row = self.client.get(f'{TENANCIES}{self.tenancy.pk}/').data['current_contract']
        self.assertIsNotNone(row['sent_at'])
        self.assertIsNone(row['signed_at'])
        self.assertEqual(row['signer_name'], '')

        self.assertEqual(self.sign().status_code, status.HTTP_200_OK)
        row = self.client.get(f'{TENANCIES}{self.tenancy.pk}/').data['current_contract']
        self.assertIsNotNone(row['signed_at'])
        self.assertIn('כהן', row['signer_name'])


class SignedLinkClosesTests(SigningTestCase):
    """A signed contract's link shows it for 30 days, then only says it was signed."""

    def test_the_signed_link_closes_after_thirty_days(self):
        self.send_link()
        self.assertEqual(self.sign().status_code, status.HTTP_200_OK)
        signed = self.fresh()
        self.assertEqual(signing.link_state(signed).state, signing.STATE_SIGNED)

        later = signed.signed_at + signing.SIGNED_LINK_LIFETIME + timedelta(minutes=1)
        closed = signing.link_state(signed, now=later)
        self.assertEqual(closed.state, signing.STATE_EXPIRED)
        self.assertEqual(closed.status_code, 410)
        payload = signing.public_payload(signed, closed)
        self.assertEqual(set(payload), {'state', 'message', 'version'})
