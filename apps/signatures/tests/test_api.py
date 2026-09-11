"""
The office's view of signatures: list, detail and PDF, scoped like the rest of
the CRM — managers see all, a partner sees their branches, a partner with none
sees nothing, a worker is refused.
"""
import io
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from pypdf import PdfReader
from rest_framework.test import APIClient, APITestCase

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.signatures.models import Signature
from apps.signatures.tests.helpers import make_signature, png_data_url

LIST_URL = '/api/v1/signatures/'

LIST_KEYS = {
    'id', 'kind', 'kind_label', 'signed_at', 'signer_name', 'signer_id_number',
    'family_id', 'family_name', 'business_customer_name', 'children', 'branch_name', 'document_title',
    'document_sha256', 'consents', 'pdf_url',
}
DETAIL_KEYS = LIST_KEYS | {'document_text', 'signature_image', 'ip_address', 'user_agent', 'refs'}


def _user(username, role, branches=()):
    """
    A user with this role, read back from the database.

    create_user() leaves the profile a signal created cached on the instance,
    with the default role; reading the user again picks up the role it set.
    """
    user = TestDataFactory.create_user(username, role=role)
    user = get_user_model().objects.get(pk=user.pk)
    if branches:
        user.profile.assigned_branches.add(*branches)
    return user


def _detail_url(signature):
    return f'{LIST_URL}{signature.id}/'


def _pdf_url(signature):
    return f'{LIST_URL}{signature.id}/pdf/'


class SignatureApiTestBase(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.north = TestDataFactory.create_branch(name='צפון')
        self.south = TestDataFactory.create_branch(name='דרום')
        self.north_family = TestDataFactory.create_family(name='ניסן', branch=self.north)
        self.south_family = TestDataFactory.create_family(name='כהן', branch=self.south)
        self.noa = TestDataFactory.create_child(family=self.north_family, first_name='נועה', last_name='ניסן')
        self.itai = TestDataFactory.create_child(family=self.north_family, first_name='איתי', last_name='ניסן')
        self.dan = TestDataFactory.create_child(family=self.south_family, first_name='דן', last_name='כהן')

        now = timezone.now()
        self.north_sig = make_signature(
            family=self.north_family, branch=self.north, children=[self.noa, self.itai],
            signed_at=now - timedelta(days=2), seed=1,
        )
        self.south_sig = make_signature(
            family=self.south_family, branch=self.south, children=[self.dan],
            signed_at=now - timedelta(days=1), seed=2,
            signer_name='משה כהן', signer_id_number='987654321', signer_phone='0547654321',
        )
        # A northern family registered for a southern course.
        self.cross_sig = make_signature(
            family=self.north_family, branch=self.south, children=[self.noa],
            signed_at=now, seed=3,
        )

        self.manager = _user('manager-sig@test', UserProfile.ROLE_MANAGER)
        self.partner = _user('partner-sig@test', UserProfile.ROLE_PARTNER, [self.north])
        self.south_partner = _user('partner-south@test', UserProfile.ROLE_PARTNER, [self.south])
        self.lonely_partner = _user('partner-none@test', UserProfile.ROLE_PARTNER)
        self.worker = _user('worker-sig@test', UserProfile.ROLE_WORKER)

    def _ids(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        return [row['id'] for row in response.json()['results']]


class SignatureListTests(SignatureApiTestBase):
    def test_the_list_shape_newest_first(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(LIST_URL)

        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(set(body), {'count', 'next', 'previous', 'results'})
        self.assertEqual(body['count'], 3)
        self.assertEqual(
            [row['id'] for row in body['results']],
            [str(self.cross_sig.id), str(self.south_sig.id), str(self.north_sig.id)],
        )
        row = body['results'][-1]
        self.assertEqual(set(row), LIST_KEYS)
        self.assertEqual(row['kind'], 'registration_terms')
        self.assertEqual(row['kind_label'], 'תקנון הרשמה')
        self.assertEqual(row['family_id'], str(self.north_family.id))
        self.assertEqual(row['family_name'], 'ניסן')
        self.assertEqual(row['branch_name'], 'צפון')
        self.assertEqual(
            sorted(row['children'], key=lambda c: c['full_name']),
            sorted(
                [{'id': str(self.noa.id), 'full_name': 'נועה ניסן'}, {'id': str(self.itai.id), 'full_name': 'איתי ניסן'}],
                key=lambda c: c['full_name'],
            ),
        )
        self.assertEqual(row['consents'], {'health': True, 'terms': True, 'computerized_documents': True})
        self.assertEqual(row['pdf_url'], f'/signatures/{self.north_sig.id}/pdf/')

    def test_filters(self):
        self.client.force_authenticate(self.manager)
        self.assertEqual(
            set(self._ids(self.client.get(LIST_URL, {'family': str(self.north_family.id)}))),
            {str(self.north_sig.id), str(self.cross_sig.id)},
        )
        self.assertEqual(
            self._ids(self.client.get(LIST_URL, {'child': str(self.itai.id)})), [str(self.north_sig.id)],
        )
        self.assertEqual(
            set(self._ids(self.client.get(LIST_URL, {'branch': str(self.south.id)}))),
            {str(self.south_sig.id), str(self.cross_sig.id)},
        )
        self.assertEqual(len(self._ids(self.client.get(LIST_URL, {'kind': 'registration_terms'}))), 3)
        self.assertEqual(self._ids(self.client.get(LIST_URL, {'kind': 'rental_contract'})), [])

    def test_dates(self):
        self.client.force_authenticate(self.manager)
        today = timezone.localdate()
        self.assertEqual(
            self._ids(self.client.get(LIST_URL, {'date_from': today.isoformat()})), [str(self.cross_sig.id)],
        )
        yesterday = (today - timedelta(days=1)).isoformat()
        self.assertEqual(
            self._ids(self.client.get(LIST_URL, {'date_from': yesterday, 'date_to': yesterday})),
            [str(self.south_sig.id)],
        )

    def test_search(self):
        self.client.force_authenticate(self.manager)
        self.assertEqual(self._ids(self.client.get(LIST_URL, {'search': 'משה'})), [str(self.south_sig.id)])
        self.assertEqual(self._ids(self.client.get(LIST_URL, {'search': '98765'})), [str(self.south_sig.id)])
        self.assertEqual(self._ids(self.client.get(LIST_URL, {'search': '0547654'})), [str(self.south_sig.id)])
        self.assertEqual(self._ids(self.client.get(LIST_URL, {'search': 'איתי'})), [str(self.north_sig.id)])
        self.assertEqual(self._ids(self.client.get(LIST_URL, {'search': 'דן כהן'})), [str(self.south_sig.id)])
        # A child's name on two signings still lists each once.
        self.assertEqual(
            self._ids(self.client.get(LIST_URL, {'search': 'נועה'})),
            [str(self.cross_sig.id), str(self.north_sig.id)],
        )

    def test_malformed_filters_are_a_bad_request(self):
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.client.get(LIST_URL, {'family': 'nope'}).status_code, 400)
        self.assertEqual(self.client.get(LIST_URL, {'date_from': '11/09/2026'}).status_code, 400)

    def test_pages_of_fifty(self):
        for seed in range(10, 58):
            make_signature(family=self.north_family, branch=self.north, seed=seed)
        self.client.force_authenticate(self.manager)

        body = self.client.get(LIST_URL).json()
        self.assertEqual(body['count'], 51)
        self.assertEqual(len(body['results']), 50)
        self.assertIsNotNone(body['next'])
        self.assertEqual(len(self.client.get(LIST_URL, {'page': 2}).json()['results']), 1)

    def test_read_only(self):
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.client.post(LIST_URL, {}, format='json').status_code, 405)
        self.assertEqual(self.client.delete(_detail_url(self.north_sig)).status_code, 405)


class SignatureScopingTests(SignatureApiTestBase):
    def test_a_partner_sees_their_branches_and_their_families(self):
        self.client.force_authenticate(self.partner)
        self.assertEqual(
            set(self._ids(self.client.get(LIST_URL))), {str(self.north_sig.id), str(self.cross_sig.id)},
        )
        self.assertEqual(self.client.get(_detail_url(self.north_sig)).status_code, 200)
        self.assertEqual(self.client.get(_detail_url(self.cross_sig)).status_code, 200)
        self.assertEqual(self.client.get(_detail_url(self.south_sig)).status_code, 404)
        self.assertEqual(self.client.get(_pdf_url(self.north_sig)).status_code, 200)
        self.assertEqual(self.client.get(_pdf_url(self.south_sig)).status_code, 404)

    def test_the_south_partner_sees_the_southern_registration_of_a_northern_family(self):
        self.client.force_authenticate(self.south_partner)
        self.assertEqual(
            set(self._ids(self.client.get(LIST_URL))), {str(self.south_sig.id), str(self.cross_sig.id)},
        )
        self.assertEqual(self.client.get(_detail_url(self.north_sig)).status_code, 404)
        self.assertEqual(self.client.get(_pdf_url(self.north_sig)).status_code, 404)

    def test_a_partner_without_branches_sees_nothing(self):
        self.client.force_authenticate(self.lonely_partner)
        body = self.client.get(LIST_URL).json()
        self.assertEqual((body['count'], body['results']), (0, []))
        self.assertEqual(self.client.get(_detail_url(self.north_sig)).status_code, 404)
        self.assertEqual(self.client.get(_pdf_url(self.north_sig)).status_code, 404)

    def test_filters_never_widen_a_partners_scope(self):
        self.client.force_authenticate(self.partner)
        self.assertEqual(self._ids(self.client.get(LIST_URL, {'family': str(self.south_family.id)})), [])
        self.assertEqual(self._ids(self.client.get(LIST_URL, {'search': 'משה'})), [])

    def test_a_worker_is_refused_everywhere(self):
        self.client.force_authenticate(self.worker)
        self.assertEqual(self.client.get(LIST_URL).status_code, 403)
        self.assertEqual(self.client.get(_detail_url(self.north_sig)).status_code, 403)
        self.assertEqual(self.client.get(_pdf_url(self.north_sig)).status_code, 403)

    def test_anonymous_is_refused_everywhere(self):
        self.assertIn(self.client.get(LIST_URL).status_code, (401, 403))
        self.assertIn(self.client.get(_detail_url(self.north_sig)).status_code, (401, 403))
        self.assertIn(self.client.get(_pdf_url(self.north_sig)).status_code, (401, 403))


class SignatureDetailTests(SignatureApiTestBase):
    def test_the_detail_shape(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(_detail_url(self.north_sig))

        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(set(body), DETAIL_KEYS)
        self.assertEqual(body['document_text'][0], '1. מחיר שנתי: המחיר הינו מחיר שנתי & חודשי.')
        self.assertEqual(body['signature_image'], png_data_url(seed=1))
        self.assertEqual(body['ip_address'], '203.0.113.9')
        self.assertEqual(body['user_agent'], 'Mozilla/5.0 (Test)')
        self.assertEqual(body['refs'], {'payment_ids': ['p-1'], 'trial': False})

    def test_an_unknown_or_malformed_id_is_not_found(self):
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.client.get(f'{LIST_URL}00000000-0000-0000-0000-000000000000/').status_code, 404)
        self.assertEqual(self.client.get(f'{LIST_URL}not-a-uuid/').status_code, 404)


class SignaturePdfTests(SignatureApiTestBase):
    def test_the_pdf_renders(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(_pdf_url(self.south_sig))

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res['Content-Type'], 'application/pdf')
        self.assertTrue(res.content.startswith(b'%PDF'))
        disposition = res['Content-Disposition']
        self.assertTrue(disposition.startswith('attachment;'))
        day = timezone.localtime(self.south_sig.signed_at).strftime('%Y-%m-%d')
        self.assertIn(f'filename="signature-{day}.pdf"', disposition)
        self.assertIn("filename*=UTF-8''signature-" + day + '-', disposition)

        text = '\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(res.content)).pages)
        self.assertIn('987654321', text)
        self.assertIn('0547654321', text)
        self.assertIn(self.south_sig.document_sha256, text)
        self.assertIn('203.0.113.9', text)

    def test_long_terms_run_onto_more_pages(self):
        paragraph = 'סעיף ארוך מאוד שחוזר על עצמו ' * 40
        signature = make_signature(
            family=self.north_family, branch=self.north, seed=9,
            document_html=''.join(f'<p>{i}. {paragraph}</p>' for i in range(30)),
        )
        self.client.force_authenticate(self.manager)
        res = self.client.get(_pdf_url(signature))

        self.assertEqual(res.status_code, 200)
        self.assertGreater(len(PdfReader(io.BytesIO(res.content)).pages), 1)

    def test_an_unreadable_image_still_renders(self):
        signature = make_signature(
            family=self.north_family, branch=self.north, seed=11, signature_png=b'\x89PNG\r\n\x1a\nbroken',
        )
        self.client.force_authenticate(self.manager)
        res = self.client.get(_pdf_url(signature))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.content.startswith(b'%PDF'))

    def test_an_ascii_signer_needs_no_fallback_name(self):
        signature = make_signature(family=self.north_family, branch=self.north, seed=12, signer_name='Ruth Nissan')
        self.client.force_authenticate(self.manager)
        res = self.client.get(_pdf_url(signature))
        day = timezone.localtime(signature.signed_at).strftime('%Y-%m-%d')
        self.assertEqual(res['Content-Disposition'], f'attachment; filename="signature-{day}-Ruth-Nissan.pdf"')
        self.assertEqual(Signature.objects.filter(pk=signature.pk).count(), 1)
