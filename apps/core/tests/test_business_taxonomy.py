"""Businesses and categories: managed by managers, and carried by customers, courses and documents."""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.models import Branch, Business, BusinessCategory, City, UserProfile
from apps.customers.models import BusinessCustomer, Child, Family
from apps.documents.models import FormalDocument

User = get_user_model()


def make_user(username, role):
    user = User.objects.create_user(username=username, password='pw-for-tests')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return User.objects.get(pk=user.pk)


class BusinessTaxonomyTests(APITestCase):
    def setUp(self):
        self.manager = make_user('manager-biz@test', UserProfile.ROLE_MANAGER)
        self.worker = make_user('worker-biz@test', UserProfile.ROLE_WORKER)
        self.partner = make_user('partner-biz@test', UserProfile.ROLE_PARTNER)
        city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='סניף', city=city)

    def test_seeded_vocabulary_exists(self):
        names = set(Business.objects.values_list('name', flat=True))
        self.assertTrue({'לקוחות', 'סוחרים', 'ספקים', 'חוגים', 'מותג קוגומלו', 'מותג געגע'} <= names)
        self.assertIn('הצגות חיצוניות', names)

    def test_every_business_can_be_invoiced_from_the_first_day(self):
        # The document wizard will not move past the business-customer step
        # until a category is picked, so a business with none is unusable.
        for business in Business.objects.all():
            self.assertTrue(business.categories.exists(), f'{business.name} — אין קטגוריה')

    def test_manager_manages_businesses_and_categories(self):
        self.client.force_authenticate(self.manager)
        res = self.client.post('/api/v1/core/businesses/', {'name': 'הופעות'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        business_id = res.data['id']
        res = self.client.post('/api/v1/core/business-categories/', {'business': business_id, 'name': 'יום הולדת'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        res = self.client.get('/api/v1/core/businesses/')
        row = next(b for b in res.data if b['id'] == business_id)
        self.assertEqual([c['name'] for c in row['categories']], ['יום הולדת'])
        res = self.client.post('/api/v1/core/businesses/', {'name': '  '}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_duplicate_says_so_in_hebrew(self):
        # 'השמירה נכשלה' with no reason is what sent the manager looking for a bug.
        self.client.force_authenticate(self.manager)
        business = Business.objects.get(name='חוגים')
        BusinessCategory.objects.get_or_create(business=business, name='קפוארה')
        res = self.client.post('/api/v1/core/business-categories/',
                               {'business': str(business.id), 'name': 'קפוארה'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('כבר קיימת קטגוריה בשם הזה בעסק הזה', str(res.data))
        res = self.client.post('/api/v1/core/businesses/', {'name': 'חוגים'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('כבר קיים עסק בשם הזה', str(res.data))

    def test_partner_may_read_but_not_write(self):
        self.client.force_authenticate(self.partner)
        self.assertEqual(self.client.get('/api/v1/core/businesses/').status_code, status.HTTP_200_OK)
        res = self.client.post('/api/v1/core/businesses/', {'name': 'X'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(self.worker)
        self.assertEqual(self.client.get('/api/v1/core/businesses/').status_code, status.HTTP_403_FORBIDDEN)

    def test_document_inherits_the_business_customers_tags(self):
        business = Business.objects.get(name='חוגים')
        category = BusinessCategory.objects.create(business=business, name='קפוארה')
        self.client.force_authenticate(self.manager)
        res = self.client.post('/api/v1/customers/business-customers/', {
            'first_name': 'בית ספר', 'last_name': 'ניצנים', 'business_id': str(business.id),
            'business_category_id': str(category.id),
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data['business_name'], 'חוגים')
        self.assertEqual(res.data['business_category_name'], 'קפוארה')
        customer_id = res.data['id']

        res = self.client.post('/api/v1/documents/documents/create-document/', {
            'document_type': 'tax_invoice', 'client_type': 'business', 'business_customer_id': customer_id,
            'invoice_details': {'document_date': '2026-09-02',
                                'line_items': [{'description': 'סדנה', 'quantity': 1, 'price': '1000'}]},
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        doc = FormalDocument.objects.get(pk=res.data['id'])
        self.assertEqual(doc.business_id, business.id)
        self.assertEqual(doc.business_category_id, category.id)
        self.assertEqual(res.data['business_name'], 'חוגים')

        # An explicit tag on the document wins over the customer's.
        other = Business.objects.get(name='מותג קוגומלו')
        res = self.client.post('/api/v1/documents/documents/create-document/', {
            'document_type': 'tax_invoice', 'client_type': 'business', 'business_customer_id': customer_id,
            'business_id': str(other.id),
            'invoice_details': {'document_date': '2026-09-02',
                                'line_items': [{'description': 'מיתוג', 'quantity': 1, 'price': '500'}]},
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(FormalDocument.objects.get(pk=res.data['id']).business_id, other.id)

    def test_period_report_groups_by_business_and_category(self):
        from apps.documents.period_report import build_report, parse_period
        business = Business.objects.get(name='חוגים')
        category = BusinessCategory.objects.create(business=business, name='קפוארה')
        fam = Family.objects.create(name='משפחה', branch=self.branch)
        kid = Child.objects.create(family=fam, first_name='נועה', last_name='כהן',
                                   birth_date=date(2015, 5, 5), gender='female', status='active')
        for number, tags in (('2026-0001', {'business': business, 'business_category': category}), ('2026-0002', {})):
            FormalDocument.objects.create(
                document_number=number, document_type='tax_invoice', client_type='existing', child=kid,
                branch=self.branch, document_date=date(2026, 9, 2), subtotal=Decimal('100'),
                vat_percent=Decimal('18'), vat_amount=Decimal('18'), total_amount=Decimal('118'), **tags,
            )
        start, end, label = parse_period({'month': '2026-09'})
        report = build_report(self.manager, start, end, label, group_by='business_unit')
        self.assertEqual([g.title for g in report.groups], ['חוגים', 'ללא שיוך לעסק'])
        report = build_report(self.manager, start, end, label, group_by='business_category')
        self.assertEqual([g.title for g in report.groups], ['חוגים · קפוארה', 'ללא קטגוריה'])

    def test_financial_dashboard_reports_revenue_by_business(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get('/api/v1/core/dashboard/financial/', {'date_from': '2026-09-01', 'date_to': '2026-09-30'})
        self.assertEqual(res.status_code, status.HTTP_200_OK, getattr(res, 'data', res.content[:200]))
        self.assertIn('revenue_by_business', res.data)


class IncomeAttributionTests(APITestCase):
    def test_every_source_lands_under_its_business(self):
        from apps.core.revenue_service import _combine_income
        lesson = {
            'by_business': {'b1': Decimal('300')}, 'by_business_name': {'b1': 'חוגים'},
            'by_category': {('b1', 'c1'): Decimal('300')}, 'by_category_name': {('b1', 'c1'): 'קפוארה'},
            'by_branch_untagged': {'br1': Decimal('1000')},
        }
        rows = _combine_income(
            lesson,
            rental_by_branch={'br1': Decimal('400'), 'br2': Decimal('50')},
            store_by_branch={'br2': Decimal('200'), '__online__': Decimal('120')},
            document_rows=[{'business_id': 'b1', 'business_name': 'חוגים', 'category_id': 'c1', 'category_name': 'קפוארה', 'amount': Decimal('1180')},
                           {'business_id': 'b1', 'business_name': 'חוגים', 'category_id': 'c1', 'category_name': 'קפוארה', 'amount': Decimal('-180')}],
            branch_names={'br1': 'פלורנטין', 'br2': 'רמת אביב'},
        )
        by_name = {r['business_name']: r for r in rows}
        # private-customer courses, rentals and pickup sales: the branch
        branches = by_name['סניפים']
        self.assertEqual(branches['revenue'], 1650.0)
        self.assertEqual({c['category_name']: c['revenue'] for c in branches['categories']}, {'פלורנטין': 1400.0, 'רמת אביב': 250.0})
        # website deliveries: the brand
        self.assertEqual(by_name['מותג קוגומלו']['categories'][0]['category_name'], 'מרצנדייס משלוחים')
        self.assertEqual(by_name['מותג קוגומלו']['revenue'], 120.0)
        # business customers and tagged courses: their own business, credits deducted
        self.assertEqual(by_name['חוגים']['revenue'], 1300.0)
        self.assertEqual(rows[0]['business_name'], 'סניפים')

    def test_live_aggregation_runs_on_an_empty_period(self):
        from apps.core.revenue_service import aggregate_income_by_business
        from datetime import date
        self.assertEqual(aggregate_income_by_business(date(2026, 1, 1), date(2026, 1, 31)), [])


SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))


class TenantIncomeAttributionTests(APITestCase):
    """
    Phase 6: a studio tenant's rent is that tenant's business's income, not a
    nameless line under their branch.

    The amount is still the calendar's — price per session × the sessions in
    the period — because that is where a rental's price lives and what its
    contract quotes. What changed is which row it lands on.
    """

    def setUp(self):
        from apps.rentals.tests.factories import make_branch, make_customer, make_rental, make_tenancy

        self.make_rental = make_rental
        self.make_tenancy = make_tenancy
        self.make_customer = make_customer
        self.branch = make_branch('פלורנטין')
        self.merchants = Business.objects.get(name='סוחרים')
        self.category = BusinessCategory.objects.create(business=self.merchants, name='סטודיו לריקוד')

    def tenant(self, name='אור', *, tagged=True):
        return self.make_customer(
            'סטודיו', name, branch=self.branch,
            business=self.merchants if tagged else None,
            business_category=self.category if tagged else None,
        )

    def rent(self, tenancy=None, *, price='400', when=date(2026, 9, 10), renter_name='שוכר'):
        return self.make_rental(
            self.branch, price=price, event_type='one_time', event_date=when,
            renter_name=renter_name, tenancy=tenancy,
            contract=(date(2026, 9, 1), date(2027, 8, 31)),
        )

    def income(self):
        from apps.core.revenue_service import aggregate_income_by_business

        return {row['business_name']: row for row in aggregate_income_by_business(*SEPTEMBER)}

    def test_a_tagged_tenants_rent_lands_under_their_business_and_category(self):
        tenancy = self.make_tenancy(self.branch, tenant=self.tenant())
        self.rent(tenancy)

        by_name = self.income()

        self.assertNotIn('סניפים', by_name)
        self.assertEqual(by_name['סוחרים']['revenue'], 400.0)
        self.assertEqual(
            [(c['category_name'], c['revenue']) for c in by_name['סוחרים']['categories']],
            [('סטודיו לריקוד', 400.0)],
        )

    def test_a_tenants_rent_and_their_invoice_are_one_line(self):
        # The point of the change: a merchant who rents a studio and is also
        # invoiced for something else reads as one business, not two sources.
        tenant = self.tenant()
        tenancy = self.make_tenancy(self.branch, tenant=tenant)
        self.rent(tenancy)
        FormalDocument.objects.create(
            document_number='2026-0100', document_type='tax_invoice', client_type='business',
            business_customer=tenant, business=self.merchants, business_category=self.category,
            branch=self.branch, document_date=date(2026, 9, 20), subtotal=Decimal('100'),
            vat_percent=Decimal('18'), vat_amount=Decimal('18'), total_amount=Decimal('118'),
        )

        row = self.income()['סוחרים']

        self.assertEqual(row['revenue'], 518.0)
        self.assertEqual(len(row['categories']), 1)
        self.assertEqual(row['categories'][0]['revenue'], 518.0)

    def test_a_rental_with_no_tenancy_is_still_the_branchs(self):
        self.rent(None)
        by_name = self.income()
        self.assertEqual(by_name['סניפים']['revenue'], 400.0)
        self.assertEqual(by_name['סניפים']['categories'][0]['category_name'], 'פלורנטין')
        self.assertNotIn('סוחרים', by_name)

    def test_a_tenant_who_was_never_tagged_is_still_the_branchs(self):
        tenancy = self.make_tenancy(self.branch, tenant=self.tenant(tagged=False))
        self.rent(tenancy)
        by_name = self.income()
        self.assertEqual(by_name['סניפים']['revenue'], 400.0)
        self.assertNotIn('סוחרים', by_name)

    def test_the_branch_panel_still_sees_every_rental_tagged_or_not(self):
        # total / by_branch_id feed the dashboard's branch table, which knows
        # nothing about documents. Phase 6 must not move a figure there.
        from apps.scheduling.studio_rental_finance import aggregate_studio_rental_revenue

        tenancy = self.make_tenancy(self.branch, tenant=self.tenant())
        self.rent(tenancy)
        self.rent(None, price='250', when=date(2026, 9, 11))

        rental = aggregate_studio_rental_revenue(*SEPTEMBER)

        self.assertEqual(rental['total'], Decimal('650'))
        self.assertEqual(rental['by_branch_id'], {str(self.branch.id): Decimal('650')})
        self.assertEqual(rental['by_month'], {'2026-09': Decimal('650')})
        # Only the untagged half is left for the branch bucket.
        self.assertEqual(rental['untagged_by_branch_id'], {str(self.branch.id): Decimal('250')})

    def _receipt_for(self, tenancy, period, *, document_date, total=Decimal('472.00')):
        """A charged month with its RT receipt, the way apps/rental_billing leaves one."""
        from apps.rental_billing.models import TenantCharge, TenantStandingOrder

        order = TenantStandingOrder.objects.create(
            tenancy=tenancy, tenant=tenancy.tenant, branch=self.branch, business=self.merchants,
            amount_before_vat=Decimal('400'), billing_day=10, start_date=date(2026, 9, 1),
        )
        doc = FormalDocument.objects.create(
            document_number='RT-2026-0001', document_type='combined', client_type='business',
            business_customer=tenancy.tenant, business=self.merchants, business_category=self.category,
            branch=self.branch, document_date=document_date, subtotal=Decimal('400'),
            vat_percent=Decimal('18'), vat_amount=Decimal('72'), total_amount=total,
        )
        TenantCharge.objects.create(
            standing_order=order, tenancy=tenancy, period=period, amount_before_vat=40000,
            vat_amount=7200, total=47200, business=self.merchants, business_category=self.category,
            status=TenantCharge.STATUS_CHARGED, trigger=TenantCharge.TRIGGER_CRON, receipt=doc,
        )
        return doc

    def test_a_month_with_its_rt_receipt_is_counted_once_by_the_receipt(self):
        tenancy = self.make_tenancy(self.branch, tenant=self.tenant())
        self.rent(tenancy)
        self._receipt_for(tenancy, date(2026, 9, 1), document_date=date(2026, 9, 10))

        row = self.income()['סוחרים']

        # The receipt's 472, not 472 + the calendar's 400.
        self.assertEqual(row['revenue'], 472.0)

    def test_a_charged_month_whose_receipt_failed_is_not_lost(self):
        from apps.rental_billing.models import TenantCharge

        tenancy = self.make_tenancy(self.branch, tenant=self.tenant())
        self.rent(tenancy)
        self._receipt_for(tenancy, date(2026, 9, 1), document_date=date(2026, 9, 10))
        TenantCharge.objects.update(receipt=None, receipt_error='הקבלה לא הופקה')
        FormalDocument.objects.all().delete()

        self.assertEqual(self.income()['סוחרים']['revenue'], 400.0)

    def test_a_receipt_issued_outside_the_period_does_not_hide_the_month(self):
        # The guard asks the one question the reports ask of a document: is its
        # own date inside this period? A receipt issued in October is October's.
        tenancy = self.make_tenancy(self.branch, tenant=self.tenant())
        self.rent(tenancy)
        self._receipt_for(tenancy, date(2026, 9, 1), document_date=date(2026, 10, 3))

        self.assertEqual(self.income()['סוחרים']['revenue'], 400.0)
