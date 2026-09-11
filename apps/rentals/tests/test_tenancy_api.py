"""The tenancy endpoints as the office uses them: create, read, change, delete and find."""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.models import Business, UserProfile
from apps.customers.models import BusinessCustomer
from apps.rentals.models import Tenancy
from apps.rentals.tests.factories import make_branch, make_customer, make_rental, make_studio, make_user

URL = '/api/v1/rentals/tenancies/'

READ_KEYS = {
    'id', 'status', 'status_label', 'branch', 'branch_name', 'monthly_amount', 'monthly_total',
    'billing_day', 'start_date', 'end_date', 'notes', 'created_at', 'suggested_monthly_amount',
    'tenant', 'slots',
}
TENANT_KEYS = {'id', 'first_name', 'last_name', 'full_name', 'company_number', 'id_number', 'phone', 'email', 'address'}
SLOT_KEYS = {
    'id', 'name', 'studio_name', 'branch_name', 'weekly_repeat_days', 'weekly_day_times',
    'start_time', 'end_time', 'price_per_session', 'is_active', 'contract_start_date',
    'contract_end_date', 'event_type', 'event_date',
}


def detail(tenancy_id, action=''):
    return f'{URL}{tenancy_id}/{action}'


class TenancyApiTestCase(APITestCase):
    def setUp(self):
        self.manager = make_user('manager-rent@test', UserProfile.ROLE_MANAGER)
        self.branch = make_branch('פלורנטין')
        self.client.force_authenticate(self.manager)

    def create(self, **payload):
        body = {'branch': str(self.branch.id), 'monthly_amount': '1000.00', **payload}
        return self.client.post(URL, body, format='json')


class CreateAndReadTests(TenancyApiTestCase):
    def test_a_new_tenant_is_a_merchant_of_the_tenancys_branch(self):
        res = self.create(
            tenant={
                'first_name': 'סטודיו', 'last_name': 'אור', 'company_number': '51-234567-8',
                'phone': '050-1234567', 'email': 'or@example.com', 'address': 'הרצל 1',
            },
            billing_day=10,
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        tenant = BusinessCustomer.objects.get(pk=res.data['tenant']['id'])
        self.assertEqual(tenant.business.name, 'סוחרים')
        self.assertEqual(tenant.branch_id, self.branch.id)
        self.assertEqual(tenant.company_number, '51-234567-8')
        self.assertEqual(res.data['tenant']['full_name'], 'סטודיו אור')
        self.assertEqual(res.data['status'], 'draft')
        self.assertEqual(res.data['status_label'], 'טיוטה')
        self.assertEqual(res.data['billing_day'], 10)
        self.assertEqual(res.data['monthly_amount'], '1000.00')
        self.assertEqual(res.data['monthly_total'], '1180.00')
        self.assertEqual(res.data['suggested_monthly_amount'], '0.00')
        self.assertEqual(res.data['branch_name'], 'פלורנטין')
        self.assertEqual(res.data['slots'], [])

    def test_read_shape(self):
        tenant = make_customer(company_number='512345678', phone='050-1234567')
        tenancy = Tenancy.objects.create(tenant=tenant, branch=self.branch, monthly_amount=Decimal('800'))
        make_rental(self.branch, price='100', days=(0, 2), studio=make_studio(self.branch), tenancy=tenancy)

        res = self.client.get(detail(tenancy.id))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(set(res.data), READ_KEYS)
        self.assertEqual(set(res.data['tenant']), TENANT_KEYS)
        self.assertEqual(len(res.data['slots']), 1)
        slot = res.data['slots'][0]
        self.assertEqual(set(slot), SLOT_KEYS)
        self.assertEqual(slot['studio_name'], 'סטודיו 1')
        self.assertEqual(slot['weekly_repeat_days'], [0, 2])
        self.assertEqual(res.data['suggested_monthly_amount'], '800.00')

        listed = self.client.get(URL)
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        # The whole list, not a page of it.
        self.assertIsInstance(listed.data, list)
        self.assertEqual(set(listed.data[0]), READ_KEYS)

    def test_a_company_tenant_needs_only_one_name(self):
        res = self.create(tenant={'first_name': 'מחול בע"מ', 'company_number': '514444444'})
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data['tenant']['full_name'], 'מחול בע"מ')

    def test_a_missing_merchants_business_leaves_the_tenant_untagged(self):
        Business.objects.filter(name='סוחרים').update(name='סוחרים לשעבר')
        with self.assertLogs('apps.rentals.tenants', level='WARNING'):
            res = self.create(tenant={'first_name': 'רון'})
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertIsNone(BusinessCustomer.objects.get(pk=res.data['tenant']['id']).business_id)
        # Never created here.
        self.assertFalse(Business.objects.filter(name='סוחרים').exists())

    def test_an_existing_tenant_keeps_its_own_tags(self):
        customer = make_customer(company_number='512345678')
        res = self.create(tenant_id=str(customer.id))
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data['tenant']['id'], str(customer.id))
        customer.refresh_from_db()
        self.assertIsNone(customer.business_id)
        self.assertEqual(BusinessCustomer.objects.count(), 1)

    def test_a_tenant_is_required_and_only_one_way(self):
        customer = make_customer()
        self.assertEqual(self.create().status_code, status.HTTP_400_BAD_REQUEST)
        both = self.create(tenant_id=str(customer.id), tenant={'first_name': 'רון'})
        self.assertEqual(both.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('tenant', both.data)
        unknown = self.create(tenant_id='00000000-0000-0000-0000-000000000000')
        self.assertEqual(unknown.status_code, status.HTTP_400_BAD_REQUEST)
        nameless = self.create(tenant={'phone': '050'})
        self.assertEqual(nameless.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Tenancy.objects.exists())
        self.assertEqual(BusinessCustomer.objects.count(), 1)

    def test_amount_day_and_dates_are_validated(self):
        tenant = {'first_name': 'רון'}
        missing = self.client.post(URL, {'branch': str(self.branch.id), 'tenant': tenant}, format='json')
        self.assertIn('monthly_amount', missing.data)
        self.assertIn('monthly_amount', self.create(tenant=tenant, monthly_amount='-1').data)
        day = self.create(tenant=tenant, billing_day=29)
        self.assertEqual(day.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(str(day.data['billing_day'][0]), 'יום החיוב חייב להיות בין 1 ל־28')
        dates = self.create(tenant=tenant, start_date='2026-10-01', end_date='2026-09-01')
        self.assertIn('end_date', dates.data)
        self.assertFalse(Tenancy.objects.exists())
        # A failed create leaves no merchant behind either.
        self.assertFalse(BusinessCustomer.objects.exists())


class ChangeTests(TenancyApiTestCase):
    def test_patch_updates_the_tenancy_and_the_linked_tenant(self):
        created = self.create(tenant={'first_name': 'רון', 'last_name': 'כהן', 'phone': '050-1111111'})
        tenancy_id = created.data['id']
        res = self.client.patch(detail(tenancy_id), {
            'tenant': {'phone': '052-2222222'},
            'status': 'sent', 'notes': 'נשלח במייל', 'monthly_amount': '1200.00',
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(res.data['tenant']['phone'], '052-2222222')
        self.assertEqual(res.data['tenant']['first_name'], 'רון')
        self.assertEqual(res.data['status_label'], 'נשלח')
        self.assertEqual(res.data['monthly_total'], '1416.00')
        self.assertEqual(BusinessCustomer.objects.count(), 1)
        self.assertEqual(BusinessCustomer.objects.get().phone, '052-2222222')

    def test_patch_can_switch_to_another_tenant(self):
        created = self.create(tenant={'first_name': 'רון'})
        other = make_customer('יעל', 'בר')
        res = self.client.patch(detail(created.data['id']), {'tenant_id': str(other.id)}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(res.data['tenant']['id'], str(other.id))

    def test_a_tenancy_holding_slots_stays_in_its_branch(self):
        tenancy = Tenancy.objects.create(tenant=make_customer(), branch=self.branch, monthly_amount=Decimal('1'))
        make_rental(self.branch, tenancy=tenancy)
        other = make_branch('רמת אביב')
        res = self.client.patch(detail(tenancy.id), {'branch': str(other.id)}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('branch', res.data)
        # Other changes to the same tenancy still go through.
        self.assertEqual(self.client.patch(detail(tenancy.id), {'notes': 'x'}, format='json').status_code, status.HTTP_200_OK)
        tenancy.slots.update(tenancy=None)
        res = self.client.patch(detail(tenancy.id), {'branch': str(other.id)}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)

    def test_put_is_not_offered(self):
        created = self.create(tenant={'first_name': 'רון'})
        res = self.client.put(detail(created.data['id']), {'monthly_amount': '1'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class DeleteTests(TenancyApiTestCase):
    def test_a_draft_with_no_slots_is_deleted_and_the_tenant_stays(self):
        created = self.create(tenant={'first_name': 'רון'})
        res = self.client.delete(detail(created.data['id']))
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Tenancy.objects.exists())
        self.assertTrue(BusinessCustomer.objects.exists())

    def test_anything_past_draft_is_kept(self):
        tenancy = Tenancy.objects.create(
            tenant=make_customer(), branch=self.branch, monthly_amount=Decimal('1'), status='signed',
        )
        res = self.client.delete(detail(tenancy.id))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data['error'], 'אפשר למחוק רק הסכם שכירות בסטטוס טיוטה')
        self.assertTrue(Tenancy.objects.filter(pk=tenancy.pk).exists())

    def test_a_draft_holding_slots_is_kept(self):
        tenancy = Tenancy.objects.create(tenant=make_customer(), branch=self.branch, monthly_amount=Decimal('1'))
        make_rental(self.branch, tenancy=tenancy)
        res = self.client.delete(detail(tenancy.id))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data['error'], 'יש לנתק את השכירויות מההסכם לפני מחיקתו')
        self.assertTrue(Tenancy.objects.filter(pk=tenancy.pk).exists())


class ListFilterTests(TenancyApiTestCase):
    def setUp(self):
        super().setUp()
        self.other_branch = make_branch('רמת אביב')
        self.or_studio = Tenancy.objects.create(
            tenant=make_customer('סטודיו', 'אור', company_number='51-234567-8', phone='050-1234567'),
            branch=self.branch, monthly_amount=Decimal('1'), status='active',
        )
        self.yoga = Tenancy.objects.create(
            tenant=make_customer('יוגה', 'בר', id_number='012345678', phone='972-52-9999999'),
            branch=self.other_branch, monthly_amount=Decimal('1'),
        )
        self.moshe = Tenancy.objects.create(
            tenant=make_customer('משה', 'פרץ'), branch=self.branch, monthly_amount=Decimal('1'),
        )

    def ids(self, **params):
        res = self.client.get(URL, params)
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        return {row['id'] for row in res.data}

    def test_by_branch_and_status(self):
        self.assertEqual(self.ids(branch=str(self.branch.id)), {str(self.or_studio.id), str(self.moshe.id)})
        self.assertEqual(self.ids(status='active'), {str(self.or_studio.id)})
        self.assertEqual(len(self.ids(status='draft,active')), 3)
        self.assertEqual(len(self.ids(branch='all')), 3)
        self.assertEqual(self.client.get(URL, {'branch': 'not-a-branch'}).status_code, status.HTTP_400_BAD_REQUEST)

    def test_search_by_name(self):
        self.assertEqual(self.ids(search='אור'), {str(self.or_studio.id)})
        self.assertEqual(self.ids(search='משה פרץ'), {str(self.moshe.id)})

    def test_search_by_number_however_typed(self):
        self.assertEqual(self.ids(search='512345678'), {str(self.or_studio.id)})
        self.assertEqual(self.ids(search='51-234567-8'), {str(self.or_studio.id)})
        self.assertEqual(self.ids(search='012345678'), {str(self.yoga.id)})
        self.assertEqual(self.ids(search='0501234567'), {str(self.or_studio.id)})
        self.assertEqual(self.ids(search='+972 50-123-4567'), {str(self.or_studio.id)})
        # A phone stored with 972 is found by the local number.
        self.assertEqual(self.ids(search='052-9999999'), {str(self.yoga.id)})
