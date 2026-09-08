"""Payment links: the payer's flow, the callback rails, the CRM, and where the money shows."""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core.models import Branch, Business, BusinessCategory, UserProfile
from apps.customers.models import TranzilaTransaction
from apps.payment_links.models import PaymentLink, PaymentLinkOption, PaymentLinkPayment
from apps.payment_links.public_views import ATTEMPT_CAP

LINKS_URL = '/api/v1/payment-links/links/'
CALLBACK_URL = '/api/v1/payment-links/public/callback/'
API_BASE = 'https://api.example.test'


def _user(role, username):
    User = get_user_model()
    user = User.objects.create_user(username=username, email=username, password='x')
    UserProfile.objects.update_or_create(user=user, defaults={'role': role})
    return user


def _client_for(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
    return client


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        self.branch = Branch.objects.create(name='Main')
        self.business = Business.objects.create(name='עסק בדיקה')
        self.category = BusinessCategory.objects.create(business=self.business, name='מופעים')
        self.other_business = Business.objects.create(name='עסק אחר')
        self.manager = _user(UserProfile.ROLE_MANAGER, 'm@test.com')
        self.client = _client_for(self.manager)
        self.link = PaymentLink.objects.create(
            title='מופע סוף שנה', description='כרטיסים', business=self.business,
            business_category=self.category, branch=self.branch, created_by=self.manager,
        )
        self.single = PaymentLinkOption.objects.create(link=self.link, label='כרטיס', amount=Decimal('50.00'), sort_order=0)
        self.pair = PaymentLinkOption.objects.create(link=self.link, label='זוג', amount=Decimal('90.00'), sort_order=1)
        self.public = APIClient()

    def _start(self, **body):
        payload = {'option_id': str(self.single.id), 'payer_name': 'דנה כהן', 'payer_phone': '0501234567'}
        payload.update(body)
        return self.public.post(f'/api/v1/payment-links/public/{self.link.slug}/start/', payload, format='json')

    def _callback(self, row, *, response='000', total=None, index='12345', signature=None):
        data = {
            'Response': response, 'sum': str(total if total is not None else row.amount), 'index': index,
            'ConfirmationCode': '0000123', 'ccno': '4580', 'cardtype': '2', 'pdesc': str(row.id).replace('-', ''),
        }
        headers = {'HTTP_X_TRANZILA_SIGNATURE': signature} if signature else {}
        return self.public.post(CALLBACK_URL, data, **headers)


@override_settings(CRM_API_BASE_URL=API_BASE)
class PayerFlowTest(_Base):
    def test_the_payer_sees_title_and_active_options_only(self):
        PaymentLinkOption.objects.create(link=self.link, label='ישן', amount=Decimal('10'), is_active=False)
        res = self.public.get(f'/api/v1/payment-links/public/{self.link.slug}/')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['title'], 'מופע סוף שנה')
        self.assertEqual([o['label'] for o in res.data['options']], ['כרטיס', 'זוג'])
        self.assertNotIn('business', res.data)

    def test_unknown_and_closed_links(self):
        self.assertEqual(self.public.get('/api/v1/payment-links/public/nope/').status_code, 404)
        self.link.is_active = False
        self.link.save()
        res = self.public.get(f'/api/v1/payment-links/public/{self.link.slug}/')
        self.assertEqual(res.status_code, 410)
        self.assertEqual(self._start().status_code, 410)
        self.link.is_active = True
        self.link.expires_at = timezone.now() - timedelta(minutes=1)
        self.link.save()
        self.assertEqual(self.public.get(f'/api/v1/payment-links/public/{self.link.slug}/').status_code, 410)

    def test_start_locks_the_amount_and_returns_the_iframe(self):
        with patch('apps.payment_links.public_views.TranzilaService.iframe') as iframe:
            iframe.return_value.create_payment_request.return_value = 'https://direct.tranzila.com/x/iframenew.php?a=1'
            res = self._start(option_id=str(self.pair.id))
        self.assertEqual(res.status_code, 200, res.content)
        row = PaymentLinkPayment.objects.get(id=res.data['payment_id'])
        self.assertEqual(row.amount, Decimal('90.00'))
        self.assertEqual(row.status, 'pending')
        self.assertEqual(row.option_label, 'זוג')
        self.assertEqual(res.data['iframe_url'], 'https://direct.tranzila.com/x/iframenew.php?a=1')
        kwargs = iframe.return_value.create_payment_request.call_args.kwargs
        self.assertEqual(kwargs['amount'], Decimal('90.00'))
        self.assertEqual(kwargs['transaction_id'], str(row.id))
        self.assertEqual(kwargs['callback_url'], f'{API_BASE}/api/v1/payment-links/public/callback/')
        self.assertIn(f'/pay/{self.link.slug}/result?p={row.id}&r=ok', kwargs['success_url'])

    def test_start_validation(self):
        self.assertEqual(self._start(option_id='nope').status_code, 400)
        self.assertEqual(self._start(payer_name='').status_code, 400)
        self.assertEqual(self._start(payer_phone='12').status_code, 400)
        self.assertEqual(self._start(payer_email='not-an-email').status_code, 400)
        self.assertEqual(PaymentLinkPayment.objects.count(), 0)

    def test_handshake_failure_marks_the_row_failed_and_answers_503(self):
        with patch('apps.payment_links.public_views.TranzilaService.iframe') as iframe:
            iframe.return_value.create_payment_request.side_effect = RuntimeError('handshake')
            res = self._start()
        self.assertEqual(res.status_code, 503)
        self.assertEqual(PaymentLinkPayment.objects.get().status, 'failed')

    @override_settings(CRM_API_BASE_URL='')
    def test_no_public_api_base_refuses_before_creating_anything(self):
        res = self._start()
        self.assertEqual(res.status_code, 503)
        self.assertEqual(PaymentLinkPayment.objects.count(), 0)

    def test_attempt_cap_per_phone_across_instances(self):
        for _ in range(ATTEMPT_CAP):
            row = PaymentLinkPayment.objects.create(
                link=self.link, option=self.single, amount=50, payer_name='x', payer_phone='0501234567', status='pending',
            )
            PaymentLinkPayment.objects.filter(id=row.id).update(created_at=timezone.now() - timedelta(minutes=3))
        # A parent still typing (a fresh pending row) is not an attempt.
        PaymentLinkPayment.objects.create(
            link=self.link, option=self.single, amount=50, payer_name='x', payer_phone='0529999999', status='pending',
        )
        with patch('apps.payment_links.public_views.TranzilaService.iframe') as iframe:
            iframe.return_value.create_payment_request.return_value = 'https://direct.tranzila.com/x/iframenew.php'
            self.assertEqual(self._start().status_code, 429)
            self.assertEqual(self._start(payer_phone='0529999999').status_code, 200)

    def test_status_poll(self):
        row = PaymentLinkPayment.objects.create(link=self.link, option=self.single, amount=50, payer_name='x', payer_phone='0501234567')
        res = self.public.get(f'/api/v1/payment-links/public/payments/{row.id}/status/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'pending')
        self.assertEqual(res.data['link_title'], 'מופע סוף שנה')


@override_settings(CRM_API_BASE_URL=API_BASE)
class CallbackRailsTest(_Base):
    """Tranzila's ledger says yes in these tests; the verification itself is covered below."""

    def setUp(self):
        super().setUp()
        self.row = PaymentLinkPayment.objects.create(
            link=self.link, option=self.single, option_label='כרטיס', amount=Decimal('50.00'),
            payer_name='דנה', payer_phone='0501234567',
        )
        verifier = patch('apps.payment_links.public_views.verify_transaction_with_tranzila', return_value=('verified', {}))
        verifier.start()
        self.addCleanup(verifier.stop)

    def test_matching_sum_completes_with_one_transaction(self):
        res = self._callback(self.row)
        self.assertEqual(res.status_code, 200, res.content)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'completed')
        self.assertEqual(self.row.card_last4, '4580')
        self.assertEqual(self.row.gateway_transaction_id, '12345')
        self.assertIsNotNone(self.row.paid_at)
        self.assertEqual(TranzilaTransaction.objects.count(), 1)
        self.assertEqual(self.row.tranzila_transaction.idempotency_key, f'paylink_{self.row.id}_12345')

    def test_a_retry_is_acknowledged_and_changes_nothing(self):
        self._callback(self.row)
        res = self._callback(self.row)
        self.assertEqual(res.data['message'], 'Already processed')
        self.assertEqual(TranzilaTransaction.objects.count(), 1)

    def test_a_different_sum_goes_to_review_not_income(self):
        res = self._callback(self.row, total='40.00')
        self.assertEqual(res.status_code, 200)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'review')
        self.assertEqual(self.row.reported_amount, Decimal('40.00'))
        self.assertEqual(self.row.amount, Decimal('50.00'))
        self.assertIn('amount_mismatch', self.row.review_reason)

    def test_a_decline_marks_failed_and_a_later_success_still_completes(self):
        self._callback(self.row, response='033')
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'failed')
        self.assertEqual(self.row.failure_code, '033')
        self._callback(self.row, index='67890')
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'completed')

    def test_a_completed_row_is_never_downgraded(self):
        self._callback(self.row)
        self._callback(self.row, response='033', index='99999')
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'completed')

    def test_unknown_or_missing_pdesc(self):
        res = self.public.post(CALLBACK_URL, {'Response': '000', 'sum': '50', 'pdesc': 'not-a-uuid'})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data['success'])
        res = self.public.post(CALLBACK_URL, {'Response': '000', 'sum': '50'})
        self.assertFalse(res.data['success'])

    @override_settings(TRANZILA_WEBHOOK_SECRET='s3cret')
    def test_a_bad_signature_is_refused(self):
        with patch('apps.payment_links.public_views.TranzilaService.iframe') as iframe:
            iframe.return_value.parse_webhook_response.return_value = {'is_successful': True, 'amount': Decimal('50')}
            iframe.return_value.verify_webhook_signature.return_value = False
            res = self._callback(self.row, signature='bad')
        self.assertEqual(res.status_code, 400)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'pending')


class CrmTest(_Base):
    def test_partner_and_worker_cannot_reach_links(self):
        partner = _user(UserProfile.ROLE_PARTNER, 'p@test.com')
        partner.profile.assigned_branches.add(self.branch)
        self.assertEqual(_client_for(partner).get(LINKS_URL).status_code, 403)
        worker = _user(UserProfile.ROLE_WORKER, 'w@test.com')
        self.assertEqual(_client_for(worker).get(LINKS_URL).status_code, 403)
        self.assertEqual(APIClient().get(LINKS_URL).status_code, 401)

    def test_create_with_options_and_public_url(self):
        res = self.client.post(LINKS_URL, {
            'title': 'קייטנה', 'description': '', 'business': str(self.business.id),
            'business_category': str(self.category.id), 'branch': None, 'is_active': True, 'expires_at': None,
            'options': [{'label': 'שבוע', 'amount': '450.00'}, {'label': 'שבועיים', 'amount': '800.00'}],
        }, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(len(res.data['options']), 2)
        self.assertIn(f"/pay/{res.data['slug']}", res.data['public_url'])
        self.assertTrue(res.data['is_open'])
        self.assertEqual(PaymentLink.objects.get(id=res.data['id']).created_by, self.manager)

    def test_validation_business_required_and_category_must_match(self):
        base = {'title': 'x', 'options': [{'label': 'a', 'amount': '10'}], 'is_active': True}
        self.assertEqual(self.client.post(LINKS_URL, {**base, 'business': None}, format='json').status_code, 400)
        res = self.client.post(LINKS_URL, {**base, 'business': str(self.other_business.id),
                                            'business_category': str(self.category.id)}, format='json')
        self.assertEqual(res.status_code, 400)
        res = self.client.post(LINKS_URL, {**base, 'business': str(self.business.id), 'options': []}, format='json')
        self.assertEqual(res.status_code, 400)
        res = self.client.post(LINKS_URL, {**base, 'business': str(self.business.id),
                                            'options': [{'label': 'a', 'amount': '0.50'}]}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_update_deactivates_omitted_options_instead_of_deleting(self):
        PaymentLinkPayment.objects.create(link=self.link, option=self.pair, amount=90, payer_name='x', status='completed', paid_at=timezone.now())
        res = self.client.patch(f'{LINKS_URL}{self.link.id}/', {
            'options': [{'id': str(self.single.id), 'label': 'כרטיס בודד', 'amount': '55.00'}],
        }, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.pair.refresh_from_db()
        self.single.refresh_from_db()
        self.assertFalse(self.pair.is_active)
        self.assertEqual(self.single.label, 'כרטיס בודד')
        self.assertEqual(self.single.amount, Decimal('55.00'))
        self.assertEqual(PaymentLinkOption.objects.filter(link=self.link).count(), 2)

    def test_list_carries_totals_and_review_count(self):
        PaymentLinkPayment.objects.create(link=self.link, option=self.single, amount=50, payer_name='a', status='completed', paid_at=timezone.now())
        PaymentLinkPayment.objects.create(link=self.link, option=self.single, amount=50, payer_name='b', status='completed', paid_at=timezone.now())
        PaymentLinkPayment.objects.create(link=self.link, option=self.single, amount=50, payer_name='c', status='review')
        PaymentLinkPayment.objects.create(link=self.link, option=self.single, amount=50, payer_name='d', status='pending')
        res = self.client.get(LINKS_URL)
        row = next(r for r in res.data if r['id'] == str(self.link.id))
        self.assertEqual(row['paid_count'], 2)
        self.assertEqual(Decimal(row['paid_total']), Decimal('100.00'))
        self.assertEqual(row['review_count'], 1)

    def test_payments_list_and_review_resolution(self):
        row = PaymentLinkPayment.objects.create(
            link=self.link, option=self.single, amount=50, reported_amount=40, payer_name='c', status='review',
        )
        res = self.client.get(f'{LINKS_URL}{self.link.id}/payments/', {'status': 'review'})
        self.assertEqual([r['id'] for r in res.data], [str(row.id)])
        res = self.client.post(f'{LINKS_URL}{self.link.id}/payments/{row.id}/resolve/', {'decision': 'completed'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        row.refresh_from_db()
        self.assertEqual(row.status, 'completed')
        self.assertEqual(row.amount, Decimal('40'))

    def test_delete_closes_a_link_with_payments(self):
        PaymentLinkPayment.objects.create(link=self.link, option=self.single, amount=50, payer_name='a', status='completed')
        res = self.client.delete(f'{LINKS_URL}{self.link.id}/')
        self.assertEqual(res.status_code, 200)
        self.link.refresh_from_db()
        self.assertFalse(self.link.is_active)
        empty = PaymentLink.objects.create(title='empty', business=self.business)
        self.assertEqual(self.client.delete(f'{LINKS_URL}{empty.id}/').status_code, 204)


class IncomeFoldTest(_Base):
    def test_completed_link_money_lands_under_the_links_business_and_category(self):
        from apps.core.revenue_service import aggregate_income_by_business

        today = date.today()
        PaymentLinkPayment.objects.create(link=self.link, option=self.single, amount=50, payer_name='a', status='completed', paid_at=timezone.now())
        PaymentLinkPayment.objects.create(link=self.link, option=self.pair, amount=90, payer_name='b', status='completed', paid_at=timezone.now())
        PaymentLinkPayment.objects.create(link=self.link, option=self.single, amount=50, payer_name='c', status='review')
        PaymentLinkPayment.objects.create(link=self.link, option=self.single, amount=50, payer_name='d', status='pending')
        out = aggregate_income_by_business(today - timedelta(days=1), today + timedelta(days=1))
        bucket = next(b for b in out if b['business_id'] == str(self.business.id))
        cat = next(c for c in bucket['categories'] if c['category_id'] == str(self.category.id))
        self.assertEqual(cat['revenue'], 140.0)

    def test_partner_branch_scope_excludes_other_branches_links(self):
        from apps.core.revenue_service import aggregate_income_by_business

        other = Branch.objects.create(name='Other')
        elsewhere = PaymentLink.objects.create(title='x', business=self.business, branch=other)
        PaymentLinkPayment.objects.create(link=elsewhere, amount=70, payer_name='a', status='completed', paid_at=timezone.now())
        PaymentLinkPayment.objects.create(link=self.link, amount=50, payer_name='b', status='completed', paid_at=timezone.now())
        today = date.today()
        out = aggregate_income_by_business(today - timedelta(days=1), today + timedelta(days=1), branch_ids=[self.branch.id])
        bucket = next((b for b in out if b['business_id'] == str(self.business.id)), None)
        self.assertIsNotNone(bucket)
        self.assertEqual(bucket['revenue'], 50.0)


@override_settings(CRM_API_BASE_URL=API_BASE)
class CallbackVerificationTest(_Base):
    """The notify POST is unauthenticated: only Tranzila's own transaction list makes a row income."""

    def setUp(self):
        super().setUp()
        self.row = PaymentLinkPayment.objects.create(
            link=self.link, option=self.single, option_label='כרטיס', amount=Decimal('50.00'),
            payer_name='דנה', payer_phone='0501234567',
        )

    def _post_with_ledger(self, listing, **kw):
        with patch('apps.payment_links.public_views.TranzilaService.iframe') as iframe:
            svc = iframe.return_value
            svc.parse_webhook_response.side_effect = lambda payload: __import__('apps.core.tranzila_service', fromlist=['TranzilaService']).TranzilaService().parse_webhook_response(payload)
            svc.credential_error.return_value = ''
            svc.list_all_transactions.return_value = listing
            return self._callback(self.row, **kw)

    def test_a_forged_success_without_a_ledger_match_is_review_not_income(self):
        res = self._post_with_ledger({'success': True, 'transactions': []})
        self.assertEqual(res.status_code, 200)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'review')
        self.assertEqual(self.row.review_reason, 'unverified_callback')

    def test_a_ledger_match_completes(self):
        listing = {'success': True, 'transactions': [{'index': '12345', 'sum': '50.00', 'pdesc': str(self.row.id).replace('-', '')}]}
        self._post_with_ledger(listing)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'completed')

    def test_a_ledger_row_with_another_sum_is_review(self):
        listing = {'success': True, 'transactions': [{'index': '12345', 'sum': '10.00'}]}
        self._post_with_ledger(listing)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'review')

    def test_lookup_failure_is_review(self):
        with patch('apps.payment_links.public_views.TranzilaService.iframe') as iframe:
            svc = iframe.return_value
            from apps.core.tranzila_service import TranzilaService as Real
            svc.parse_webhook_response.side_effect = Real().parse_webhook_response
            svc.credential_error.return_value = ''
            svc.list_all_transactions.side_effect = RuntimeError('down')
            self._callback(self.row)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'review')
        self.assertEqual(self.row.review_reason, 'verification_unavailable')

    def test_an_index_already_used_by_another_payment_is_review(self):
        other = PaymentLinkPayment.objects.create(link=self.link, option=self.single, amount=50, payer_name='x',
                                                  status='completed', gateway_transaction_id='12345')
        with patch('apps.payment_links.public_views.verify_transaction_with_tranzila', return_value=('verified', {})):
            self._callback(self.row)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'review')
        self.assertTrue(self.row.review_reason.startswith('index_reused'))

    def test_a_second_approved_index_on_a_completed_row_is_recorded_and_flagged(self):
        with patch('apps.payment_links.public_views.verify_transaction_with_tranzila', return_value=('verified', {})):
            self._callback(self.row)
            self._callback(self.row, index='99999')
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'completed')
        self.assertTrue(self.row.review_reason.startswith('second_charge'))
        self.assertEqual(TranzilaTransaction.objects.count(), 2)

    def test_foreign_currency_is_review(self):
        with patch('apps.payment_links.public_views.verify_transaction_with_tranzila', return_value=('verified', {})):
            data = {'Response': '000', 'sum': '50.00', 'index': '1', 'currency': '2', 'pdesc': str(self.row.id).replace('-', '')}
            self.public.post(CALLBACK_URL, data)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'review')
