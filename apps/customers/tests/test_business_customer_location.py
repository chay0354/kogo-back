"""
Where a business customer is filed: set once without a question, and changed
only with a word on how far the change goes — from now on, or the documents
already issued too.

Every name is invented.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.core.models import Branch, Business, BusinessCategory, City, UserProfile
from apps.customers.location_models import BusinessCustomerLocationChange
from apps.customers.models import BusinessCustomer
from apps.documents.models import FormalDocument

User = get_user_model()
URL = '/api/v1/customers/business-customers/'


def make_user(username, role, branches=()):
    user = User.objects.create_user(username=username, password='pw-for-tests')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    profile.assigned_branches.set(branches)
    return User.objects.get(pk=user.pk)


class LocationFixture:
    def setUp(self):
        city = City.objects.create(name='עיר')
        self.north = Branch.objects.create(name='סניף צפון', city=city)
        self.south = Branch.objects.create(name='סניף דרום', city=city)
        self.lessons, _ = Business.objects.get_or_create(name='חוגים')
        self.branches, _ = BusinessCategory.objects.get_or_create(business=self.lessons, name='סניפים')
        self.shows, _ = Business.objects.get_or_create(name='הצגות חיצוניות')
        self.general, _ = BusinessCategory.objects.get_or_create(business=self.shows, name='כללי')
        self.manager = make_user('manager-loc@test', UserProfile.ROLE_MANAGER)
        # A card as the import of the previous software leaves it: no location at all.
        self.customer = BusinessCustomer.objects.create(first_name='הפקות', last_name='הדגמה', company_number='515000001')
        self.client.force_authenticate(self.manager)

    def at_north(self):
        return {'business_id': str(self.lessons.pk), 'business_category_id': str(self.branches.pk),
                'branch_id': str(self.north.pk)}

    def at_south(self):
        return {**self.at_north(), 'branch_id': str(self.south.pk)}

    def at_shows(self):
        return {'business_id': str(self.shows.pk), 'business_category_id': str(self.general.pk), 'branch_id': None}

    def file(self, where, customer=None, **extra):
        customer = customer or self.customer
        return self.client.post(f'{URL}{customer.pk}/location/', {**where, **extra}, format='json')

    def document(self, number, customer=None, **where):
        customer = customer or self.customer
        return FormalDocument.objects.create(
            document_number=number, document_type='tax_invoice', client_type='business',
            business_customer=customer, document_date=date(2026, 10, 7), total_amount=Decimal('118.00'), **where,
        )

    def filed(self, document):
        document.refresh_from_db()
        return (document.business, document.business_category, document.branch)


class FirstLocationTests(LocationFixture, APITestCase):
    def test_a_clean_card_is_filed_without_a_question(self):
        res = self.file(self.at_north())

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data['changed'], res.data['scope'], res.data['documents_changed']), (True, 'first', 0))
        self.customer.refresh_from_db()
        self.assertEqual(
            (self.customer.business, self.customer.business_category, self.customer.branch),
            (self.lessons, self.branches, self.north),
        )
        # The names the wizard reads are kept beside the ids.
        self.assertEqual((self.customer.business_type, self.customer.category), ('חוגים', 'סניפים'))
        self.assertEqual(res.data['customer']['branch_name'], 'סניף צפון')
        record = BusinessCustomerLocationChange.objects.get()
        self.assertEqual((record.scope, record.previous_label, record.new_label, record.changed_by),
                         ('first', '', 'חוגים · סניפים · סניף צפון', self.manager))

    def test_the_same_location_again_is_nothing(self):
        self.file(self.at_north())
        res = self.file(self.at_north())
        self.assertEqual((res.status_code, res.data['changed']), (200, False))
        self.assertEqual(BusinessCustomerLocationChange.objects.count(), 1)

    def test_what_does_not_belong_together_is_refused(self):
        cases = {
            'no business': {'business_id': None, 'business_category_id': str(self.general.pk), 'branch_id': None},
            'no category': {'business_id': str(self.shows.pk), 'business_category_id': None, 'branch_id': None},
            'category of another business': {**self.at_shows(), 'business_category_id': str(self.branches.pk)},
            'a branch outside סניפים': {**self.at_shows(), 'branch_id': str(self.north.pk)},
            'not an id': {**self.at_shows(), 'business_id': 'x'},
            'a scope that is not one': {**self.at_shows(), 'scope': 'sideways'},
        }
        for name, where in cases.items():
            with self.subTest(name):
                res = self.file(where)
                self.assertEqual(res.status_code, 400, res.data)
                self.assertIn('error', res.data)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.business)
        self.assertFalse(BusinessCustomerLocationChange.objects.exists())


class ChangedLocationTests(LocationFixture, APITestCase):
    def setUp(self):
        super().setUp()
        self.file(self.at_north())
        self.old = self.document('TI-2026-000001', business=self.lessons, business_category=self.branches,
                                 branch=self.north)
        self.older = self.document('TI-2026-000002', business=self.lessons, business_category=self.branches)
        self.draft = self.document('D-AAAA1111')

    def test_a_change_without_a_word_on_how_far_is_asked_back(self):
        res = self.file(self.at_shows())

        self.assertEqual(res.status_code, 409)
        self.assertEqual((res.data['needs_scope'], res.data['documents']), (True, 3))
        self.assertEqual(res.data['previous']['label'], 'חוגים · סניפים · סניף צפון')
        self.assertEqual(res.data['location']['label'], 'הצגות חיצוניות · כללי')
        # Nothing moved while the question is open.
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.branch, self.north)
        self.assertEqual(self.filed(self.old), (self.lessons, self.branches, self.north))

    def test_from_now_on_changes_the_card_and_leaves_what_was_issued(self):
        res = self.file(self.at_shows(), scope='future')

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data['scope'], res.data['documents_changed']), ('future', 0))
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.business, self.customer.business_category, self.customer.branch),
                         (self.shows, self.general, None))
        self.assertEqual(self.filed(self.old), (self.lessons, self.branches, self.north))
        self.assertEqual(self.filed(self.draft), (None, None, None))
        record = BusinessCustomerLocationChange.objects.first()
        self.assertEqual((record.scope, record.documents, record.documents_changed), ('future', [], 0))

    def test_backwards_too_moves_every_document_and_remembers_where_each_was(self):
        res = self.file(self.at_south(), scope='all')

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data['scope'], res.data['documents_changed']), ('all', 3))
        for document in (self.old, self.older, self.draft):
            self.assertEqual(self.filed(document), (self.lessons, self.branches, self.south))
        # Only where it is filed moved: the number and the amount are as issued.
        self.old.refresh_from_db()
        self.assertEqual((self.old.document_number, self.old.total_amount), ('TI-2026-000001', Decimal('118.00')))
        record = BusinessCustomerLocationChange.objects.first()
        self.assertEqual((record.scope, record.documents_changed, record.new_label),
                         ('all', 3, 'חוגים · סניפים · סניף דרום'))
        before = {row['number']: row for row in record.documents}
        self.assertEqual(before['TI-2026-000001']['branch_id'], str(self.north.pk))
        self.assertEqual(
            (before['D-AAAA1111']['business_id'], before['D-AAAA1111']['branch_id']), (None, None),
        )

    def test_backwards_leaves_rent_receipts_michals_and_other_customers_alone(self):
        rent = self.document('RT-2026-000001', business=self.lessons, business_category=self.branches,
                             branch=self.north)
        michal = self.document('MK-2026-000001', branch=self.north)
        other = BusinessCustomer.objects.create(first_name='אחר', last_name='לגמרי')
        theirs = self.document('TI-2026-000009', customer=other, branch=self.north)

        res = self.file(self.at_south(), scope='all')

        self.assertEqual(res.data['documents_changed'], 3)
        self.assertEqual(self.filed(rent)[2], self.north)
        self.assertEqual(self.filed(michal)[2], self.north)
        self.assertEqual(self.filed(theirs)[2], self.north)
        # The question counts what it would move, too.
        self.assertEqual(self.file(self.at_north()).data['documents'], 3)

    def test_a_document_already_there_is_not_counted_as_moved(self):
        self.document('TI-2026-000003', business=self.lessons, business_category=self.branches, branch=self.south)
        res = self.file(self.at_south(), scope='all')
        self.assertEqual(res.data['documents_changed'], 3)

    def test_the_history_is_never_edited(self):
        record = BusinessCustomerLocationChange.objects.get()
        record.new_label = 'שונה'
        with self.assertRaises(ValueError):
            record.save()


class LocationPermissionTests(LocationFixture, APITestCase):
    def test_a_partner_changes_their_own_merchant_from_now_on_and_never_backwards(self):
        partner = make_user('partner-loc@test', UserProfile.ROLE_PARTNER, [self.north])
        mine = BusinessCustomer.objects.create(
            first_name='שלי', last_name='סוחר', business=self.lessons, business_category=self.branches,
            branch=self.north, business_type='חוגים', category='סניפים',
        )
        self.document('TI-2026-000001', customer=mine, branch=self.north)
        self.client.force_authenticate(partner)

        # Moving issued documents changes where income is counted: the office's alone.
        self.assertEqual(self.file(self.at_north(), customer=mine, scope='all').status_code, 403)
        # A card with no branch is the office's; another branch is not theirs to file under.
        self.assertEqual(self.file(self.at_north()).status_code, 404)
        self.assertEqual(self.file(self.at_south(), customer=mine, scope='future').status_code, 403)

    def test_a_worker_and_nobody_at_all(self):
        self.client.force_authenticate(make_user('worker-loc@test', UserProfile.ROLE_WORKER))
        self.assertEqual(self.file(self.at_north()).status_code, 403)
        self.client.force_authenticate(None)
        self.assertIn(self.file(self.at_north()).status_code, (401, 403))


class ImportedCardTests(LocationFixture, APITestCase):
    def test_a_card_imported_clean_has_no_location_until_the_office_files_it(self):
        from apps.legacy_import import service
        from apps.legacy_import.models import LegacyImport
        from apps.legacy_import.tests.helpers import row

        legacy_import = LegacyImport.objects.create(
            file_name='t.xls', sha256='3' * 64, row_count=1,
            rows=[row(ext_number='5', doc_type='tax_invoice', first_name='לקוח', last_name='מיובא',
                      location='סניף צפון')],
        )
        # No mapping sent: the office files each customer itself.
        service.commit(legacy_import.pk, {}, False, self.manager, import_documents=False)
        card = BusinessCustomer.objects.get(last_name='מיובא')
        self.assertEqual((card.business, card.business_category, card.branch, card.business_type, card.category),
                         (None, None, None, '', ''))

        res = self.file(self.at_north(), customer=card)
        self.assertEqual((res.status_code, res.data['scope']), (200, 'first'))
