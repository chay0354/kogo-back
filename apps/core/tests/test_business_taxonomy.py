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
        self.assertEqual(by_name['מותג קוגומלו']['categories'][0]['category_name'], 'משלוחים')
        self.assertEqual(by_name['מותג קוגומלו']['revenue'], 120.0)
        # business customers and tagged courses: their own business, credits deducted
        self.assertEqual(by_name['חוגים']['revenue'], 1300.0)
        self.assertEqual(rows[0]['business_name'], 'סניפים')

    def test_live_aggregation_runs_on_an_empty_period(self):
        from apps.core.revenue_service import aggregate_income_by_business
        from datetime import date
        self.assertEqual(aggregate_income_by_business(date(2026, 1, 1), date(2026, 1, 31)), [])
