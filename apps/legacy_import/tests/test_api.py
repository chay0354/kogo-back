"""Preview and commit through the API, against the database."""
import json
from datetime import date
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from apps.core.models import Branch, Business, BusinessCategory, City, UserProfile
from apps.customers.models import BusinessCustomer, Family, Parent
from apps.legacy_import import service
from apps.legacy_import.models import LegacyDocument, LegacyImport
from apps.legacy_import.tests.helpers import CORRUPT_XLS, FIXTURE_PASSWORD, make_user, row

BASE = '/api/v1/legacy-import/'


def upload(path=CORRUPT_XLS, name='export_invoices.xls'):
    return SimpleUploadedFile(name, path.read_bytes(), content_type='application/vnd.ms-excel')


class ImportFixture:
    def setUp(self):
        self.manager = make_user('manager-legacy@test', UserProfile.ROLE_MANAGER)
        city = City.objects.create(name='עיר')
        self.kfar_saba = Branch.objects.create(name='כפר סבא', city=city)
        self.zamir = Branch.objects.create(name='מרכז זמיר', city=city)
        # The businesses are seeded by core's migrations; the סניפים category is not.
        self.lessons, _ = Business.objects.get_or_create(name='חוגים')
        self.branches_category, _ = BusinessCategory.objects.get_or_create(business=self.lessons, name='סניפים')
        self.shows, _ = Business.objects.get_or_create(name='הצגות חיצוניות')
        self.shows_general, _ = BusinessCategory.objects.get_or_create(business=self.shows, name='כללי')
        self.client.force_authenticate(self.manager)

    def branch_target(self, branch):
        return {'business_id': str(self.lessons.pk), 'category_id': str(self.branches_category.pk),
                'branch_id': str(branch.pk)}

    def stored_import(self, rows):
        return LegacyImport.objects.create(file_name='t.xls', sha256='0' * 64, row_count=len(rows), rows=rows)

    def commit(self, legacy_import, mapping=None, parents=False):
        return self.client.post(
            f'{BASE}{legacy_import.pk}/commit/',
            {'mapping': mapping or {}, 'include_subscription_parents': parents}, format='json',
        )


class PermissionTests(ImportFixture, APITestCase):
    def test_only_a_manager(self):
        partner = make_user('partner-legacy@test', UserProfile.ROLE_PARTNER)
        worker = make_user('worker-legacy@test', UserProfile.ROLE_WORKER)
        legacy_import = self.stored_import([row()])
        for user in (partner, worker):
            self.client.force_authenticate(user)
            with self.subTest(user.username):
                self.assertEqual(self.client.post(f'{BASE}preview/', {'file': upload()}).status_code, 403)
                self.assertEqual(self.commit(legacy_import).status_code, 403)
                self.assertEqual(self.client.get(f'{BASE}documents/', {'q': 'x'}).status_code, 403)
                self.assertEqual(self.client.get(f'{BASE}series/').status_code, 403)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(f'{BASE}series/').status_code, (401, 403))
        self.assertEqual(LegacyDocument.objects.count(), 0)


class PreviewTests(ImportFixture, APITestCase):
    def test_the_preview_of_the_export(self):
        res = self.client.post(f'{BASE}preview/', {'file': upload()})

        self.assertEqual(res.status_code, 201, res.data)
        summary = res.data['summary']
        self.assertEqual(summary['documents']['total'], 6)
        self.assertEqual(
            {t['doc_type']: (t['count'], t['first_number'], t['last_number']) for t in summary['types']},
            {'combined': (2, 70001, 70002), 'tax_invoice': (1, 40001, 40001), 'receipt': (1, 33001, 33001),
             'transaction_invoice': (1, 60001, 60001), 'credit_invoice': (1, 41001, 41001)},
        )
        customers = summary['customers']
        self.assertEqual((customers['total'], customers['business'], customers['parents']), (3, 2, 1))
        self.assertEqual(customers['business_create'], 2)
        locations = {entry['location']: entry for entry in summary['locations']}
        self.assertEqual(locations['כפר סבא']['branch_id'], str(self.kfar_saba.pk))
        self.assertEqual(locations['כפר סבא']['category_id'], str(self.branches_category.pk))
        self.assertEqual(locations['21 הצגות מופעים ופסטיבלים במותג']['business_id'], str(self.shows.pk))
        self.assertEqual({b['name'] for b in summary['options']['branches']}, {'כפר סבא', 'מרכז זמיר'})
        # The preview wrote the import and nothing else.
        self.assertEqual(LegacyImport.objects.get().status, 'preview')
        self.assertEqual(LegacyDocument.objects.count(), 0)
        self.assertEqual(BusinessCustomer.objects.count(), 0)
        self.assertNotIn('rows', res.data)

    def test_the_password_is_never_stored_or_returned(self):
        res = self.client.post(f'{BASE}preview/', {'file': upload()})
        legacy_import = LegacyImport.objects.get()
        for blob in (json.dumps(legacy_import.rows, ensure_ascii=False),
                     json.dumps(legacy_import.summary, ensure_ascii=False),
                     json.dumps(res.data, ensure_ascii=False, default=str)):
            self.assertNotIn(FIXTURE_PASSWORD, blob)
            self.assertNotIn('01/01/1980', blob)
        self.assertFalse(any('password' in key for r in legacy_import.rows for key in r))

    def test_a_file_over_the_limit_is_refused_before_it_is_read(self):
        big = SimpleUploadedFile('big.xls', b'\0' * (service.MAX_UPLOAD_BYTES + 1))
        res = self.client.post(f'{BASE}preview/', {'file': big})
        self.assertEqual(res.status_code, 400)
        self.assertIn('גדול מדי', res.data['error'])
        self.assertEqual(LegacyImport.objects.count(), 0)

    def test_a_file_that_is_not_the_export(self):
        res = self.client.post(f'{BASE}preview/', {'file': SimpleUploadedFile('x.xls', b'hello')})
        self.assertEqual(res.status_code, 400)
        self.assertIn('קובץ', res.data['error'])
        self.assertEqual(self.client.post(f'{BASE}preview/', {}).status_code, 400)

    def test_existing_cards_and_families_are_recognised(self):
        BusinessCustomer.objects.create(first_name='מתנ"ס', last_name='הדגמה', company_number='512345678')
        family = Family.objects.create(name='משפחת בדיקה', branch=self.kfar_saba)
        Parent.objects.create(family=family, first_name='נועה', last_name='בדיקה', phone='050-0000001')
        summary = self.client.post(f'{BASE}preview/', {'file': upload()}).data['summary']
        customers = summary['customers']
        self.assertEqual((customers['business_create'], customers['business_update']), (1, 1))
        self.assertEqual(customers['parents_matching_family'], 1)
        matched = next(c for c in customers['business_list'] if c['action'] == 'update')
        self.assertEqual(matched['match']['how'], 'לפי ת"ז / ח"פ')


class CommitTests(ImportFixture, APITestCase):
    def test_the_whole_export_imported(self):
        preview = self.client.post(f'{BASE}preview/', {'file': upload()}).data
        mapping = {'כפר סבא': self.branch_target(self.kfar_saba),
                   '21 הצגות מופעים ופסטיבלים במותג': {'business_id': str(self.shows.pk),
                                                       'category_id': str(self.shows_general.pk)}}

        res = self.client.post(f"{BASE}{preview['id']}/commit/", {'mapping': mapping}, format='json')

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['customers']['created'], 2)
        self.assertEqual(res.data['documents']['created'], 6)
        # Business customers only; the card-paying parent stays a family matter.
        self.assertEqual(
            sorted(BusinessCustomer.objects.values_list('first_name', 'last_name')),
            [('יוסי', 'דוגמה'), ('מתנ"ס', 'הדגמה')],
        )
        office = BusinessCustomer.objects.get(first_name='מתנ"ס')
        self.assertEqual((office.company_number, office.id_number), ('512345678', ''))
        showman = BusinessCustomer.objects.get(first_name='יוסי')
        self.assertEqual((showman.business, showman.business_category, showman.branch),
                         (self.shows, self.shows_general, None))
        self.assertEqual((showman.business_type, showman.category), ('הצגות חיצוניות', 'כללי'))
        self.assertEqual(showman.phone, '0521111111')
        self.assertIn('יובא מהתוכנה הקודמת: 2 מסמכים, אחרון חשבונית מס זיכוי 41001 מ-02/04/2025', showman.notes)
        self.assertEqual(LegacyDocument.objects.filter(business_customer=office).count(), 2)
        parent_doc = LegacyDocument.objects.get(doc_type='combined', number=70001)
        self.assertIsNone(parent_doc.business_customer)
        self.assertEqual((parent_doc.business, parent_doc.business_category, parent_doc.branch),
                         (self.lessons, self.branches_category, self.kfar_saba))
        self.assertEqual(parent_doc.receipt_total, Decimal('236.00'))
        self.assertEqual(LegacyImport.objects.get().status, 'committed')

    def test_committing_twice_changes_nothing_new(self):
        preview = self.client.post(f'{BASE}preview/', {'file': upload()}).data
        mapping = {'כפר סבא': self.branch_target(self.kfar_saba)}
        self.client.post(f"{BASE}{preview['id']}/commit/", {'mapping': mapping}, format='json')
        cards = {c.pk: (c.notes, c.updated_at) for c in BusinessCustomer.objects.all()}
        docs = {d.pk: d.updated_at for d in LegacyDocument.objects.all()}

        again = self.client.post(f"{BASE}{preview['id']}/commit/", {'mapping': mapping}, format='json').data
        # The same file uploaded anew is the same answer.
        second = self.client.post(f'{BASE}preview/', {'file': upload()}).data
        self.assertEqual(second['summary']['documents']['already_imported'], 6)
        self.assertEqual(second['summary']['customers']['business_update'], 2)
        third = self.client.post(f"{BASE}{second['id']}/commit/", {'mapping': mapping}, format='json').data

        for result in (again, third):
            self.assertEqual(result['customers']['created'], 0)
            self.assertEqual(result['customers']['updated'], 0)
            self.assertEqual(result['documents']['created'], 0)
            self.assertEqual(result['documents']['updated'], 0)
            self.assertEqual(result['documents']['unchanged'], 6)
        self.assertEqual({c.pk: (c.notes, c.updated_at) for c in BusinessCustomer.objects.all()}, cards)
        self.assertEqual({d.pk: d.updated_at for d in LegacyDocument.objects.all()}, docs)
        self.assertTrue(all(notes.count('יובא מהתוכנה הקודמת') == 1 for notes, _ in cards.values()))

    def test_subscription_parents_only_when_asked(self):
        legacy_import = self.stored_import([row(ext_number='7', first_name='הורה', last_name='בדיקה')])
        self.assertEqual(self.commit(legacy_import).data['customers']['created'], 0)
        self.assertEqual(BusinessCustomer.objects.count(), 0)
        self.assertEqual(self.commit(legacy_import, parents=True).data['customers']['created'], 1)
        self.assertEqual(LegacyDocument.objects.get().business_customer, BusinessCustomer.objects.get())

    def test_a_parent_left_out_is_linked_to_a_card_only_by_the_same_id(self):
        same_person = BusinessCustomer.objects.create(first_name='מדריך', last_name='עצמאי', id_number='12345678')
        legacy_import = self.stored_import([
            # A business customer, and a parent who pays with the same office email.
            row(ext_number='1', doc_type='tax_invoice', number=1, date='2025-06-01',
                first_name='מתנ"ס', last_name='הדגמה', email='office@example.test'),
            row(ext_number='2', number=2, date='2025-01-01', first_name='רכזת', last_name='בדיקה',
                email='office@example.test'),
            # A parent whose ת"ז is on a card already.
            row(id_number='012345678', number=3, first_name='מדריך', last_name='עצמאי'),
        ])
        first = self.commit(legacy_import).data
        self.assertEqual(first['customers']['parents_linked_to_existing_cards'], 1)
        self.assertIsNone(LegacyDocument.objects.get(number=2).business_customer)
        self.assertEqual(LegacyDocument.objects.get(number=3).business_customer, same_person)
        again = self.commit(legacy_import).data
        self.assertEqual((again['documents']['updated'], again['customers']['updated']), (0, 0))

    def test_an_existing_card_is_matched_its_blanks_filled_and_its_name_the_newest(self):
        card = BusinessCustomer.objects.create(
            first_name='סטודיו', last_name='ישן', company_number='51-234567-8', email='', phone='',
            notes='לקוח ותיק',
        )
        legacy_import = self.stored_import([
            row(id_number='512345678', doc_type='tax_invoice', date='2024-01-01', number=100,
                first_name='סטודיו', last_name='ישן', email='old@example.test'),
            row(id_number='512345678', doc_type='tax_invoice', date='2025-01-01', number=101,
                first_name='סטודיו', last_name='חדש', email='studio@example.test', phone='0500000009',
                location='כפר סבא'),
        ])

        result = self.commit(legacy_import, {'כפר סבא': self.branch_target(self.kfar_saba)}).data

        self.assertEqual((result['customers']['created'], result['customers']['updated']), (0, 1))
        card.refresh_from_db()
        self.assertEqual((card.first_name, card.last_name), ('סטודיו', 'חדש'))
        self.assertEqual((card.email, card.phone), ('studio@example.test', '0500000009'))
        self.assertEqual(card.company_number, '51-234567-8')  # not blank, so not touched
        self.assertEqual((card.business, card.business_category, card.branch),
                         (self.lessons, self.branches_category, self.kfar_saba))
        self.assertTrue(card.notes.startswith('לקוח ותיק\nיובא מהתוכנה הקודמת: 2 מסמכים'))
        self.assertEqual(LegacyDocument.objects.filter(business_customer=card).count(), 2)

    def test_matching_by_email_then_by_phone_and_name(self):
        by_email = BusinessCustomer.objects.create(first_name='א', last_name='ב', email='Same@Example.test')
        by_phone = BusinessCustomer.objects.create(first_name='להקת', last_name='הדגמה', phone='050-000-0003')
        stranger = BusinessCustomer.objects.create(first_name='אחר', last_name='לגמרי', phone='0500000004')
        legacy_import = self.stored_import([
            row(ext_number='1', doc_type='tax_invoice', email='same@example.test', first_name='א', last_name='ב'),
            row(ext_number='2', doc_type='tax_invoice', phone='0500000003', first_name='להקת', last_name='הדגמה'),
            row(ext_number='3', doc_type='tax_invoice', phone='0500000004', first_name='שם', last_name='שונה'),
        ])
        result = self.commit(legacy_import).data
        self.assertEqual(result['customers']['created'], 1)
        self.assertEqual(LegacyDocument.objects.get(customer_key='ext:1').business_customer, by_email)
        self.assertEqual(LegacyDocument.objects.get(customer_key='ext:2').business_customer, by_phone)
        self.assertNotEqual(LegacyDocument.objects.get(customer_key='ext:3').business_customer, stranger)

    def test_the_latest_documents_location_decides_the_card(self):
        legacy_import = self.stored_import([
            row(ext_number='8', doc_type='tax_invoice', date='2024-01-01', location='כפר סבא'),
            row(ext_number='8', doc_type='tax_invoice', date='2025-06-01', location='סניף כפר גנים ג פ"ת'),
        ])
        self.commit(legacy_import, {'כפר סבא': self.branch_target(self.kfar_saba),
                                    'סניף כפר גנים ג פ"ת': self.branch_target(self.zamir)})
        card = BusinessCustomer.objects.get()
        self.assertEqual(card.branch, self.zamir)
        # Each document keeps the place it was issued at.
        self.assertEqual(
            sorted(LegacyDocument.objects.values_list('document_date', 'branch__name')),
            [(date(2024, 1, 1), 'כפר סבא'), (date(2025, 6, 1), 'מרכז זמיר')],
        )

    def test_an_organisation_in_the_first_name_is_split_like_the_wizard_splits_it(self):
        legacy_import = self.stored_import([
            row(ext_number='1', doc_type='tax_invoice', first_name='עיריית עיר הדגמה', last_name=''),
            row(ext_number='2', doc_type='tax_invoice', first_name='אולפנית', last_name=''),
        ])
        self.commit(legacy_import)
        self.assertEqual(
            sorted(BusinessCustomer.objects.values_list('first_name', 'last_name')),
            [('אולפנית', ''), ('עיריית', 'עיר הדגמה')],
        )
        # A one-word organisation saves again from the documents wizard as it is.
        card = BusinessCustomer.objects.get(first_name='אולפנית')
        res = self.client.patch(f'/api/v1/customers/business-customers/{card.pk}/',
                                {'first_name': 'אולפנית', 'last_name': ''}, format='json')
        self.assertEqual(res.status_code, 200, res.data)

    def test_a_customer_deleted_in_the_old_software_gets_no_card(self):
        legacy_import = self.stored_import([row(ext_number='4', doc_type='tax_invoice', deleted=True)])
        result = self.commit(legacy_import).data
        self.assertEqual(result['customers']['skipped_deleted'], 1)
        self.assertEqual(BusinessCustomer.objects.count(), 0)
        self.assertEqual(LegacyDocument.objects.count(), 1)

    def test_a_mapping_that_points_nowhere_is_refused_and_writes_nothing(self):
        legacy_import = self.stored_import([row(doc_type='tax_invoice')])
        other = Business.objects.create(name='אחר')
        bad = [
            {'כפר סבא': {'branch_id': '00000000-0000-0000-0000-000000000000'}},
            {'כפר סבא': {'business_id': str(other.pk), 'category_id': str(self.branches_category.pk)}},
            {'כפר סבא': {'business_id': 'not-a-uuid'}},
        ]
        for mapping in bad:
            with self.subTest(mapping):
                res = self.commit(legacy_import, mapping)
                self.assertEqual(res.status_code, 400)
                self.assertTrue(res.data['error'])
        self.assertEqual(LegacyDocument.objects.count(), 0)
        self.assertEqual(BusinessCustomer.objects.count(), 0)
        self.assertEqual(LegacyImport.objects.get().status, 'preview')

    def test_a_newer_export_updates_what_changed(self):
        first = self.stored_import([row(ext_number='1', doc_type='tax_invoice', number=5, remark='')])
        self.commit(first)
        second = self.stored_import([row(ext_number='1', doc_type='tax_invoice', number=5, remark='שולם'),
                                     row(ext_number='1', doc_type='tax_invoice', number=6, date='2025-02-01')])
        result = self.commit(second).data
        self.assertEqual((result['documents']['created'], result['documents']['updated']), (1, 1))
        self.assertEqual(LegacyDocument.objects.get(number=5).remark, 'שולם')
        self.assertEqual(LegacyDocument.objects.get(number=5).source_import, second)
        self.assertEqual(BusinessCustomer.objects.count(), 1)


class ReadBackTests(ImportFixture, APITestCase):
    def setUp(self):
        super().setUp()
        legacy_import = self.stored_import([
            row(id_number='512345678', doc_type='tax_invoice', number=40001, date='2024-01-01', details='הדרכה'),
            row(id_number='512345678', doc_type='receipt', number=33001, date='2025-01-01'),
            row(ext_number='9', number=70001, date='2025-03-01', first_name='הורה', phone='0500000007'),
            row(ext_number='9', number=70005, date='2025-02-01', first_name='הורה', phone='0500000007'),
        ])
        self.commit(legacy_import)
        self.card = BusinessCustomer.objects.get()

    def test_a_customers_history_newest_first(self):
        res = self.client.get(f'{BASE}documents/', {'business_customer': str(self.card.pk)})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['count'], 2)
        self.assertEqual([d['number'] for d in res.data['results']], [33001, 40001])
        self.assertEqual(res.data['results'][0]['source'], 'legacy')
        self.assertEqual(res.data['results'][1]['doc_type_label'], 'חשבונית מס')

    def test_search(self):
        def numbers(q):
            return [d['number'] for d in self.client.get(f'{BASE}documents/', {'q': q}).data['results']]
        self.assertEqual(numbers('הדרכה'), [40001])
        self.assertEqual(numbers('70005'), [70005])
        self.assertEqual(numbers('0500000007'), [70001, 70005])
        self.assertEqual(numbers('512345678'), [33001, 40001])
        self.assertEqual(self.client.get(f'{BASE}documents/').status_code, 400)
        self.assertEqual(self.client.get(f'{BASE}documents/', {'business_customer': 'x'}).status_code, 400)

    def test_the_last_number_per_type(self):
        series = {s['doc_type']: s for s in self.client.get(f'{BASE}series/').data['series']}
        self.assertEqual((series['combined']['last_number'], series['combined']['last_date']), (70005, '2025-02-01'))
        self.assertEqual(series['combined']['latest_date'], '2025-03-01')
        self.assertEqual(series['receipt']['last_number'], 33001)
        self.assertEqual(series['combined']['original_labels'], ['חשבונית מס קבלה'])

    def test_the_import_list_and_one_import(self):
        listing = self.client.get(BASE).data
        self.assertEqual(listing[0]['status'], 'committed')
        self.assertNotIn('rows', listing[0])
        one = self.client.get(f"{BASE}{listing[0]['id']}/").data
        self.assertEqual(one['result']['documents']['created'], 4)
        self.assertNotIn('rows', one)
