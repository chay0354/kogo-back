"""
"סניפים" chosen as the business of a charge or a link (owner, 7.10.2026).

The office picks the business "סניפים" in the business-charge window, the card
link window or the payment link window, then the branch — and the money is that
branch's. A branch must be named; a category under "סניפים" is an extra. In the
reports the money lands on the branch's line of the one "סניפים" business,
beside the branch's courses — never on a second "סניפים" line.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.core import system_audit
from apps.core.models import Branch, Business, BusinessCategory, UserProfile
from apps.core.revenue_service import BRANCHES_BUSINESS_KEY, BRANCHES_BUSINESS_LABEL, aggregate_income_by_business
from apps.customers.models import BusinessCustomer, Child, Family, Payment
from apps.documents.models import FormalDocument
from apps.documents.numbering import israel_today
from apps.documents.period_report import build_report
from apps.documents.undocumented_income import _link_tags
from apps.payment_links.business_charge import (
    BusinessChargeDocumentError,
    issue_business_charge_document,
    validate_business_charge_link,
)
from apps.payment_links.models import CardLink, PaymentLink, PaymentLinkOption, PaymentLinkPayment

CHARGE = '/api/v1/payment-links/links/business-charge/'
LINKS = '/api/v1/payment-links/links/'
CARD_LINKS = '/api/v1/customers/card-links/'


def manager_client(email):
    user = get_user_model().objects.create_user(username=email, email=email, password='x')
    UserProfile.objects.update_or_create(user=user, defaults={'role': UserProfile.ROLE_MANAGER})
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Token {Token.objects.create(user=user).key}')
    return user, client


class Fixture(TestCase):
    def setUp(self):
        self.manager, self.client = manager_client('branches-links@test.local')
        self.north = Branch.objects.create(name='סניף צפון')
        self.south = Branch.objects.create(name='סניף דרום')
        # The migration adds the business; a category under it is the office's own.
        self.our_branches = Business.objects.get(name='סניפים')
        self.events, _ = BusinessCategory.objects.get_or_create(business=self.our_branches, name='אירועים')
        self.shows = Business.objects.create(name='תיאטרון הדגמה')
        self.general = BusinessCategory.objects.create(business=self.shows, name='כללי')
        self.customer = BusinessCustomer.objects.create(
            first_name='גן', last_name='הדגמה', company_number='515151515',
            phone='0501234567', email='payer@example.test',
        )

    def today_range(self):
        today = date.today()
        return today - timedelta(days=1), today + timedelta(days=1)

    def branches_bucket(self, **scope):
        """The one "סניפים" business of the income report — fails if there are two."""
        out = aggregate_income_by_business(*self.today_range(), **scope)
        named = [bucket for bucket in out if bucket['business_name'] == BRANCHES_BUSINESS_LABEL]
        self.assertEqual(len(named), 1, [bucket['business_id'] for bucket in out])
        self.assertEqual(named[0]['business_id'], BRANCHES_BUSINESS_KEY)
        return {row['category_name']: row['revenue'] for row in named[0]['categories']}


@override_settings(TRANZILA_BILLING_TERMINAL='', DOCUMENT_SIGNING_ENABLED=False)
class BusinessChargeUnderBranchesTests(Fixture):
    def charge(self, **extra):
        data = {
            'business_customer_id': str(self.customer.id),
            'business_id': str(self.our_branches.id),
            'branch_id': str(self.north.id),
            'amount': '118.00',
            'description': 'אירוע בסניף',
        }
        data.update(extra)
        return self.client.post(CHARGE, data, format='json')

    def test_a_branch_and_no_category_is_a_whole_charge(self):
        res = self.charge()
        self.assertEqual(res.status_code, 201, res.data)
        link = PaymentLink.objects.get(pk=res.data['id'])
        self.assertEqual((link.business, link.branch, link.business_category), (self.our_branches, self.north, None))
        validate_business_charge_link(link, Decimal('118.00'))

    def test_without_a_branch_it_is_refused(self):
        res = self.charge(branch_id=None)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(str(res.data['branch_id'][0]), 'יש לבחור סניף')
        self.assertFalse(PaymentLink.objects.exists())

    def test_a_category_of_the_branches_business_may_be_added_and_no_other(self):
        res = self.charge(business_category_id=str(self.events.id))
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(PaymentLink.objects.get(pk=res.data['id']).business_category, self.events)
        res = self.charge(business_category_id=str(self.general.id))
        self.assertEqual(res.status_code, 400)
        self.assertEqual(str(res.data['business_category_id'][0]), 'הקטגוריה אינה שייכת לעסק שנבחר')

    def test_every_other_business_still_needs_its_category(self):
        res = self.charge(business_id=str(self.shows.id))
        self.assertEqual(res.status_code, 400)
        self.assertEqual(str(res.data['business_category_id'][0]), 'יש לבחור קטגוריה')
        # …and still goes without a branch, as before.
        res = self.charge(business_id=str(self.shows.id), business_category_id=str(self.general.id), branch_id=None)
        self.assertEqual(res.status_code, 201, res.data)

    def test_a_link_left_without_a_branch_takes_no_payment(self):
        link = PaymentLink.objects.create(
            kind=PaymentLink.KIND_BUSINESS_CHARGE, title='x', business=self.our_branches,
            business_customer=self.customer,
        )
        with self.assertRaisesMessage(BusinessChargeDocumentError, 'יש לבחור סניף'):
            validate_business_charge_link(link, Decimal('10'))

    def test_the_document_and_its_income_are_the_branchs(self):
        link = PaymentLink.objects.get(pk=self.charge().data['id'])
        payment = PaymentLinkPayment.objects.create(
            link=link, option=link.options.get(), option_label='אירוע', amount=Decimal('118.00'),
            payer_name=self.customer.full_name, status=PaymentLinkPayment.STATUS_COMPLETED,
            gateway_transaction_id='77', gateway_confirmation_code='0001234', card_last4='4580',
            card_type='Visa', paid_at=timezone.now(),
        )

        doc = issue_business_charge_document(payment.id)

        self.assertEqual(doc.document_type, 'combined')
        self.assertEqual((doc.business, doc.branch, doc.business_category), (self.our_branches, self.north, None))
        # Counted once — through the document — on the branch's line.
        day = israel_today()
        out = aggregate_income_by_business(day - timedelta(days=1), day + timedelta(days=1))
        named = [bucket for bucket in out if bucket['business_name'] == BRANCHES_BUSINESS_LABEL]
        self.assertEqual([bucket['business_id'] for bucket in named], [BRANCHES_BUSINESS_KEY])
        self.assertEqual(
            [(row['category_name'], row['revenue']) for row in named[0]['categories']], [('סניף צפון', 118.0)],
        )


class PaymentLinkUnderBranchesTests(Fixture):
    def body(self, **extra):
        data = {
            'title': 'מופע סניף', 'description': '', 'business': str(self.our_branches.id),
            'business_category': None, 'branch': str(self.north.id), 'is_active': True, 'expires_at': None,
            'options': [{'label': 'כרטיס', 'amount': '50.00'}],
        }
        data.update(extra)
        return data

    def test_a_link_under_branches_names_its_branch(self):
        res = self.client.post(LINKS, self.body(), format='json')
        self.assertEqual(res.status_code, 201, res.content)
        res = self.client.post(LINKS, self.body(branch=None), format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(str(res.data['branch'][0]), 'יש לבחור סניף')

    def test_another_business_still_goes_without_a_branch(self):
        res = self.client.post(LINKS, self.body(business=str(self.shows.id), branch=None), format='json')
        self.assertEqual(res.status_code, 201, res.content)

    def test_a_link_cannot_be_moved_under_branches_without_a_branch(self):
        link = PaymentLink.objects.create(title='x', business=self.shows)
        PaymentLinkOption.objects.create(link=link, label='a', amount=Decimal('10'))
        res = self.client.patch(f'{LINKS}{link.id}/', {'business': str(self.our_branches.id)}, format='json')
        self.assertEqual(res.status_code, 400)
        res = self.client.patch(
            f'{LINKS}{link.id}/', {'business': str(self.our_branches.id), 'branch': str(self.south.id)}, format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)

    def test_a_link_saved_before_the_rule_is_still_closed_and_renamed(self):
        link = PaymentLink.objects.create(title='ישן', business=self.our_branches)
        res = self.client.patch(f'{LINKS}{link.id}/', {'is_active': False, 'title': 'ישן וסגור'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        link.refresh_from_db()
        self.assertEqual((link.is_active, link.title), (False, 'ישן וסגור'))

    def test_the_money_lands_on_the_branchs_line(self):
        link = PaymentLink.objects.create(title='מופע', business=self.our_branches, branch=self.north)
        with_category = PaymentLink.objects.create(
            title='אירוע', business=self.our_branches, business_category=self.events, branch=self.south,
        )
        elsewhere = PaymentLink.objects.create(
            title='הצגה', business=self.shows, business_category=self.general, branch=self.north,
        )
        for row, amount in ((link, 50), (link, 90), (with_category, 70), (elsewhere, 30)):
            PaymentLinkPayment.objects.create(
                link=row, amount=amount, payer_name='a', status='completed', paid_at=timezone.now(),
            )

        self.assertEqual(self.branches_bucket(), {'סניף צפון': 140.0, 'סניף דרום': 70.0})
        # A partner of the north branch sees the north's line only.
        self.assertEqual(self.branches_bucket(branch_ids=[self.north.id]), {'סניף צפון': 140.0})
        # Another business is where it was: its own line, its own category.
        out = aggregate_income_by_business(*self.today_range())
        shows = next(bucket for bucket in out if bucket['business_id'] == str(self.shows.id))
        self.assertEqual([(row['category_name'], row['revenue']) for row in shows['categories']], [('כללי', 30.0)])

    def test_the_weekly_check_wants_a_branch_and_not_a_category(self):
        PaymentLink.objects.create(title='תקין', business=self.our_branches, branch=self.north, is_active=True)
        self.assertEqual(system_audit.probe_payment_links().severity, 'green')
        PaymentLink.objects.create(title='בלי סניף', business=self.our_branches, is_active=True)
        result = system_audit.probe_payment_links()
        self.assertEqual((result.severity, len(result.rows)), ('red', 1))

    def test_the_weekly_check_still_wants_a_category_elsewhere(self):
        PaymentLink.objects.create(title='בלי קטגוריה', business=self.shows, branch=self.north, is_active=True)
        self.assertEqual(system_audit.probe_payment_links().severity, 'red')


class CardLinkUnderBranchesTests(Fixture):
    def setUp(self):
        super().setUp()
        self.family = Family.objects.create(name='כהן', phone='0501111111', branch=self.north)
        self.child = Child.objects.create(
            family=self.family, first_name='נועה', last_name='כהן', birth_date=date(2016, 1, 1),
            gender='female', status='active',
        )

    def one_time(self, child=None, **extra):
        data = {
            'kind': 'one_time', 'child_id': str((child or self.child).id), 'amount': '150.00',
            'description': 'חולצת סניף', 'business_id': str(self.our_branches.id),
        }
        data.update(extra)
        return self.client.post(CARD_LINKS, data, format='json')

    def test_the_familys_branch_stands_when_none_is_picked(self):
        res = self.one_time()
        self.assertEqual(res.status_code, 201, res.content)
        link = CardLink.objects.get(pk=res.data['id'])
        self.assertEqual((link.business, link.branch, link.business_category), (self.our_branches, self.north, None))

    def test_a_picked_branch_wins(self):
        res = self.one_time(branch_id=str(self.south.id), business_category_id=str(self.events.id))
        self.assertEqual(res.status_code, 201, res.content)
        link = CardLink.objects.get(pk=res.data['id'])
        self.assertEqual((link.branch, link.business_category), (self.south, self.events))

    def test_a_family_with_no_branch_must_have_one_picked(self):
        family = Family.objects.create(name='לוי', phone='0502222222')
        child = Child.objects.create(
            family=family, first_name='דן', last_name='לוי', birth_date=date(2015, 1, 1),
            gender='male', status='active',
        )
        res = self.one_time(child=child)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['error'], 'יש לבחור סניף')
        self.assertFalse(CardLink.objects.filter(child=child).exists())
        self.assertEqual(self.one_time(child=child, branch_id=str(self.south.id)).status_code, 201)
        # With no business at all nothing new is asked: the charge is made as before.
        self.assertEqual(self.one_time(child=child, business_id=None).status_code, 201)

    def test_the_money_lands_on_the_branchs_line(self):
        link = CardLink.objects.create(
            kind=CardLink.KIND_ONE_TIME, child=self.child, amount=Decimal('150.00'), description='חולצה',
            branch=self.north, business=self.our_branches, created_by=self.manager,
        )
        payment = Payment.objects.create(
            child=self.child, family=self.family, branch=self.north,
            payment_type='one_time', status='completed', base_amount=150, discount_amount=0, final_amount=150,
            payment_date=timezone.now(),
        )
        CardLink.objects.filter(pk=link.pk).update(payment=payment)
        self.assertEqual(self.branches_bucket(), {'סניף צפון': 150.0})


class ReportsUnderBranchesTests(Fixture):
    """The period report and the income-without-a-document report follow the same line."""

    def test_a_link_under_branches_carries_no_tag_of_its_own(self):
        under_branches = PaymentLink(business=self.our_branches, business_category=self.events, branch=self.north)
        self.assertEqual(_link_tags(under_branches), {})
        elsewhere = PaymentLink(business=self.shows, business_category=self.general)
        self.assertEqual(
            _link_tags(elsewhere),
            {'business_id': self.shows.id, 'business_name': 'תיאטרון הדגמה',
             'category_id': self.general.id, 'category_name': 'כללי'},
        )
        self.assertEqual(_link_tags(PaymentLink()), {})

    def document(self, number, **tags):
        return FormalDocument.objects.create(
            document_number=number, document_type='tax_invoice', client_type='business',
            business_customer=self.customer, document_date=date(2026, 8, 9), subtotal=Decimal('100.00'),
            vat_amount=Decimal('18.00'), total_amount=Decimal('118.00'), **tags,
        )

    def groups(self, group_by):
        report = build_report(self.manager, date(2026, 8, 1), date(2026, 8, 31), 'אוגוסט 2026', group_by=group_by)
        return {group.title: (group.key, [row.document_number for row in group.rows]) for group in report.groups}

    def test_the_period_report_files_the_document_on_the_branchs_line(self):
        from apps.customers.financial_models import Invoice

        self.document('TI-2026-000001', business=self.our_branches, branch=self.north)
        self.document('TI-2026-000002', business=self.our_branches, business_category=self.events, branch=self.north)
        self.document('TI-2026-000003', business=self.shows, business_category=self.general)
        # A lesson receipt of the same branch: the line the documents must share.
        family = Family.objects.create(name='כהן', branch=self.north)
        Invoice.objects.create(
            invoice_number='IR-2026-000001', family=family, branch=self.north, amount=Decimal('236.00'),
            status='paid', payment_method='credit_card', payment_type='recurring', payer_name='כהן',
            invoice_date=datetime(2026, 8, 9, 12, tzinfo=dt_timezone.utc),
        )

        by_unit = self.groups('business_unit')
        self.assertEqual(sorted(by_unit), [BRANCHES_BUSINESS_LABEL, 'תיאטרון הדגמה'])
        key, numbers = by_unit[BRANCHES_BUSINESS_LABEL]
        self.assertEqual(key, BRANCHES_BUSINESS_KEY)
        self.assertEqual(sorted(numbers), ['IR-2026-000001', 'TI-2026-000001', 'TI-2026-000002'])

        by_category = self.groups('business_category')
        self.assertEqual(sorted(by_category), ['סניפים · סניף צפון', 'תיאטרון הדגמה · כללי'])
        self.assertEqual(
            sorted(by_category['סניפים · סניף צפון'][1]), ['IR-2026-000001', 'TI-2026-000001', 'TI-2026-000002'],
        )
