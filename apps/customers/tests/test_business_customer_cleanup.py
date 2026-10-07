"""
Business customers that are one customer: found, and made one card.

One studio under a card per branch it rents in; a customer who changed name.
The office merges them — or says they are different customers — and nothing a
card knew is lost with it.

Every name is invented.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from rest_framework.test import APITestCase

from apps.core.models import Branch, Business, BusinessCategory, City, UserProfile
from apps.customers.business_customer_cleanup import name_key, without_branch
from apps.customers.cleanup_models import BusinessCustomerCleanup
from apps.customers.models import BusinessCustomer
from apps.documents.models import FormalDocument

User = get_user_model()
URL = '/api/v1/customers/business-customers/'
IMPORTED = 'יובא מהתוכנה הקודמת: פרטי הלקוח בלבד, בלי המסמכים. בתוכנה הקודמת הופקו לו 3 מסמכים, אחרון חשבונית מס 40001 מ-'


def make_user(username, role):
    user = User.objects.create_user(username=username, password='pw-for-tests')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return User.objects.get(pk=user.pk)


def card(first, last, last_seen='', **fields):
    notes = fields.pop('notes', '') or (IMPORTED + last_seen if last_seen else '')
    return BusinessCustomer.objects.create(first_name=first, last_name=last, notes=notes, **fields)


class NameTests(SimpleTestCase):
    def test_the_branch_is_taken_off_the_end_of_a_name(self):
        self.assertEqual(without_branch('אולפן הדגמה בית ספר למשחק סניף מינץ'), 'אולפן הדגמה בית ספר למשחק')
        self.assertEqual(without_branch('אולפן הדגמה - סניף ראש העין'), 'אולפן הדגמה')
        self.assertEqual(without_branch('אולפן הדגמה (סניף כפר סבא)'), 'אולפן הדגמה')
        self.assertEqual(without_branch('מתנ״ס  הדגמה'), 'מתנ"ס הדגמה')
        # A name that is all branch, or has the word inside it, is left alone.
        self.assertEqual(without_branch('סניף מרכז'), 'סניף מרכז')
        self.assertEqual(without_branch('רשת סניפים בע"מ'), 'רשת סניפים בע"מ')

    def test_names_are_compared_without_branch_brackets_spacing_or_case(self):
        self.assertEqual(name_key('אולפן הדגמה סניף מינץ'), name_key('אולפן  הדגמה סניף ראש העין'))
        self.assertEqual(name_key('דנה מדריכה (היפהופ)'), name_key('דנה מדריכה'))
        self.assertNotEqual(name_key('מתנ"ס צפון'), name_key('מתנ"ס דרום'))


class CleanupFixture:
    def setUp(self):
        self.manager = make_user('manager-clean@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)

    def groups(self):
        res = self.client.get(f'{URL}duplicates/')
        self.assertEqual(res.status_code, 200, res.data)
        return res.data['groups']

    def merge(self, survivor, others, **extra):
        return self.client.post(
            f'{URL}{survivor.pk}/merge/', {'merge_ids': [str(c.pk) for c in others], **extra}, format='json',
        )


class SuggestionTests(CleanupFixture, APITestCase):
    def test_one_customer_under_a_card_per_branch_is_found_and_named_without_the_branch(self):
        mintz = card('אולפן', 'הדגמה בית ספר למשחק סניף מינץ', '31/07/2026', company_number='558000001')
        rosh = card('אולפן', 'הדגמה בית ספר למשחק סניף ראש העין', '15/07/2026', company_number='558000001')
        kfar = card('אולפן', 'הדגמה בית ספר למשחק סניף כפר סבא', '01/06/2026')
        card('אחר', 'לגמרי', '01/01/2026', company_number='512000009')

        groups = self.groups()

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual((group['reason'], group['recommended']), ('same_name', True))
        self.assertEqual(group['suggested_name'], 'אולפן הדגמה בית ספר למשחק')
        # Newest first, with what the office needs to tell them apart.
        self.assertEqual([c['id'] for c in group['cards']], [str(mintz.pk), str(rosh.pk), str(kfar.pk)])
        self.assertEqual(group['cards'][0]['last_seen'], '2026-07-31')
        self.assertEqual(group['cards'][2]['company_number'], '')

    def test_a_customer_who_changed_name_is_found_by_the_number_and_takes_the_newest_name(self):
        card('מוסדות', 'חינוך קאנטרי הדגמה', '10/07/2025', company_number='510000001')
        new = card('קהילה', 'הדגמה החדשה', '20/08/2026', company_number='51-000000-1')

        group = self.groups()[0]

        # The old name went out of use a year before: a change of name, to merge.
        self.assertEqual((group['reason'], group['recommended']), ('same_number', True))
        self.assertIn('שינוי שם', group['hint'])
        self.assertEqual(group['suggested_name'], 'קהילה הדגמה החדשה')
        self.assertEqual(group['cards'][0]['id'], str(new.pk))

    def test_one_number_under_two_names_active_side_by_side_is_shown_but_not_recommended(self):
        card('רשת', 'מתנ"ס צפון', '06/09/2026', company_number='580000001')
        card('רשת', 'מתנ"ס דרום', '19/07/2026', company_number='580000001')
        # No date known for a card: nothing says its name went out of use.
        card('עמותת', 'הדגמה', company_number='580000002')
        card('עמותת', 'הדגמה אחרת', '01/01/2020', company_number='580000002')

        groups = self.groups()

        self.assertEqual([(g['reason'], g['recommended']) for g in groups],
                         [('same_number', False), ('same_number', False)])
        self.assertIn('פעילים במקביל', groups[0]['hint'])

    def test_a_shared_phone_is_shown_to_be_looked_at_and_an_email_alone_is_not(self):
        card('אירה', 'בדיקה', '01/07/2025', phone='050-000-0001', id_number='324000001', email='office@example.test')
        card('מוריה', 'בדיקה', '01/07/2026', phone='0500000001', id_number='226000001')
        card('חן', 'אחרת', '01/07/2026', email='office@example.test')

        groups = self.groups()

        self.assertEqual([(g['reason'], g['recommended'], len(g['cards'])) for g in groups], [('same_phone', False, 2)])

    def test_the_card_with_documents_is_the_one_suggested_to_stay(self):
        old = card('אולפן', 'הדגמה סניף מינץ', '01/01/2025')
        card('אולפן', 'הדגמה סניף ראש העין', '01/07/2026')
        FormalDocument.objects.create(
            document_number='TI-2026-000001', document_type='tax_invoice', client_type='business',
            business_customer=old, document_date=date(2026, 10, 7), total_amount=Decimal('118.00'),
        )
        group = self.groups()[0]
        self.assertEqual(group['suggested_survivor_id'], str(old.pk))
        self.assertEqual(next(c for c in group['cards'] if c['id'] == str(old.pk))['documents'], 1)

    def test_the_same_cards_found_two_ways_are_one_group(self):
        card('אולפן', 'הדגמה סניף מינץ', company_number='558000001', phone='0500000002')
        card('אולפן', 'הדגמה סניף ראש העין', company_number='558000001', phone='0500000002')
        self.assertEqual([g['reason'] for g in self.groups()], ['same_name'])

    def test_cards_the_office_said_are_different_customers_are_not_suggested_again(self):
        north = card('רשת', 'מתנ"ס צפון', company_number='580000001')
        south = card('רשת', 'מתנ"ס דרום', company_number='580000001')
        self.assertEqual(len(self.groups()), 1)

        res = self.client.post(f'{URL}keep-apart/', {'card_ids': [str(north.pk), str(south.pk)]}, format='json')

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.groups(), [])
        self.assertEqual(BusinessCustomer.objects.count(), 2)
        # A third centre of the network is a new question.
        card('רשת', 'מתנ"ס מזרח', company_number='580000001')
        self.assertEqual(len(self.groups()[0]['cards']), 3)


class MergeTests(CleanupFixture, APITestCase):
    def setUp(self):
        super().setUp()
        city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='סניף צפון', city=city)
        self.lessons, _ = Business.objects.get_or_create(name='חוגים')
        self.branches, _ = BusinessCategory.objects.get_or_create(business=self.lessons, name='סניפים')
        self.mintz = card('אולפן', 'הדגמה סניף מינץ', '31/07/2026', company_number='558000001')
        self.rosh = card(
            'אולפן', 'הדגמה סניף ראש העין', '15/07/2026', company_number='558000001',
            email='studio@example.test', phone='0500000003', address='רחוב 1, עיר',
            business=self.lessons, business_category=self.branches, branch=self.branch,
            business_type='חוגים', category='סניפים',
        )
        self.kfar = card('אולפן', 'הדגמה סניף כפר סבא', '01/06/2026', id_number='301000001')

    def test_the_cards_become_one_with_the_name_given_and_nothing_they_knew_is_lost(self):
        document = FormalDocument.objects.create(
            document_number='TI-2026-000001', document_type='tax_invoice', client_type='business',
            business_customer=self.rosh, document_date=date(2026, 10, 7), total_amount=Decimal('118.00'),
        )

        res = self.merge(self.mintz, [self.rosh, self.kfar], name='אולפן הדגמה')

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data['merged'], res.data['name']), (2, 'אולפן הדגמה'))
        self.assertEqual(res.data['moved'], {'documents.FormalDocument': 1})
        self.assertEqual(list(BusinessCustomer.objects.values_list('pk', flat=True)), [self.mintz.pk])
        self.mintz.refresh_from_db()
        self.assertEqual((self.mintz.first_name, self.mintz.last_name), ('אולפן', 'הדגמה'))
        # Its own number stays; the blanks are filled from the cards that went away.
        self.assertEqual(
            (self.mintz.company_number, self.mintz.id_number, self.mintz.email, self.mintz.phone, self.mintz.address),
            ('558000001', '301000001', 'studio@example.test', '0500000003', 'רחוב 1, עיר'),
        )
        self.assertEqual((self.mintz.business, self.mintz.business_category, self.mintz.branch,
                          self.mintz.business_type, self.mintz.category),
                         (self.lessons, self.branches, self.branch, 'חוגים', 'סניפים'))
        document.refresh_from_db()
        self.assertEqual(document.business_customer, self.mintz)
        self.assertEqual(res.data['customer']['full_name'], 'אולפן הדגמה')

    def test_every_card_that_went_away_is_kept_whole_in_the_record(self):
        self.merge(self.mintz, [self.rosh, self.kfar], name='אולפן הדגמה')

        record = BusinessCustomerCleanup.objects.get()
        self.assertEqual((record.action, record.survivor, record.decided_by),
                         ('merged', self.mintz, self.manager))
        self.assertEqual((record.name_before, record.name_after), ('אולפן הדגמה סניף מינץ', 'אולפן הדגמה'))
        kept = {c['name']: c for c in record.merged_cards}
        self.assertEqual(sorted(kept), ['אולפן הדגמה סניף כפר סבא', 'אולפן הדגמה סניף ראש העין'])
        rosh = kept['אולפן הדגמה סניף ראש העין']
        self.assertEqual((rosh['email'], rosh['phone'], rosh['company_number'], rosh['branch_id']),
                         ('studio@example.test', '0500000003', '558000001', str(self.branch.pk)))
        self.assertEqual(sorted(record.card_ids), sorted(str(c.pk) for c in (self.mintz, self.rosh, self.kfar)))
        record.name_after = 'שונה'
        with self.assertRaises(ValueError):
            record.save()

    def test_without_a_name_the_card_that_stays_keeps_its_own(self):
        res = self.merge(self.rosh, [self.kfar])
        self.assertEqual(res.data['name'], 'אולפן הדגמה סניף ראש העין')
        self.rosh.refresh_from_db()
        # What it already had is not replaced by the other card's.
        self.assertEqual((self.rosh.email, self.rosh.id_number), ('studio@example.test', '301000001'))

    def test_a_tenancy_and_a_standing_order_move_with_the_customer(self):
        from apps.rentals.models import Tenancy

        tenancy_fields = {f.name for f in Tenancy._meta.concrete_fields}
        relations = {rel.related_model._meta.label for rel in BusinessCustomer._meta.related_objects}
        # Every table that points at a card is moved by the merge: a new one would be moved too.
        self.assertTrue({'rentals.Tenancy', 'rental_billing.TenantStandingOrder', 'documents.FormalDocument',
                         'payment_links.PaymentLink', 'signatures.Signature',
                         'legacy_import.LegacyDocument'} <= relations)
        self.assertIn('tenant', tenancy_fields)

    def test_the_import_finds_the_merged_card_for_every_customer_it_was_and_keeps_its_name(self):
        from apps.legacy_import import service
        from apps.legacy_import.models import LegacyImport
        from apps.legacy_import.parser import prepare_rows
        from apps.legacy_import.tests.helpers import row

        BusinessCustomer.objects.all().delete()

        def rows():
            made = [
                row(id_number='558000001', ext_number='28749', doc_type='tax_invoice', number=1,
                    date='2026-07-31', first_name='אולפן', last_name='הדגמה סניף מינץ'),
                row(id_number='558000001', ext_number='28750', doc_type='tax_invoice', number=2,
                    date='2026-07-15', first_name='אולפן', last_name='הדגמה סניף ראש העין'),
                row(ext_number='36659', doc_type='tax_invoice', number=3, date='2026-06-01',
                    first_name='אולפן', last_name='הדגמה סניף כפר סבא'),
            ]
            prepare_rows(made)
            return made

        def import_cards(sha):
            legacy_import = LegacyImport.objects.create(file_name='t.xls', sha256=sha * 64, row_count=3, rows=rows())
            return service.commit(legacy_import.pk, {}, False, self.manager, import_documents=False)

        self.assertEqual(import_cards('4')['customers']['created'], 3)
        group = self.groups()[0]
        survivor = BusinessCustomer.objects.get(pk=group['suggested_survivor_id'])
        others = [BusinessCustomer.objects.get(pk=c['id']) for c in group['cards'] if c['id'] != str(survivor.pk)]
        self.assertEqual(self.merge(survivor, others, name=group['suggested_name']).status_code, 200)

        again = import_cards('5')

        self.assertEqual(again['customers']['created'], 0)
        self.assertEqual(BusinessCustomer.objects.count(), 1)
        self.assertEqual(BusinessCustomer.objects.get().full_name, 'אולפן הדגמה')

    def test_what_cannot_be_merged_is_refused_and_nothing_changes(self):
        cases = {
            'into itself': self.merge(self.mintz, [self.mintz]),
            'nothing chosen': self.client.post(f'{URL}{self.mintz.pk}/merge/', {'merge_ids': []}, format='json'),
            'a card that is gone': self.client.post(
                f'{URL}{self.mintz.pk}/merge/',
                {'merge_ids': ['00000000-0000-0000-0000-000000000000']}, format='json',
            ),
            'an empty name': self.merge(self.mintz, [self.rosh], name='  '),
            'not an id': self.client.post(f'{URL}{self.mintz.pk}/merge/', {'merge_ids': ['x']}, format='json'),
        }
        for name, res in cases.items():
            with self.subTest(name):
                self.assertEqual(res.status_code, 400, res.data)
                self.assertIn('error', res.data)
        self.assertEqual(BusinessCustomer.objects.count(), 3)
        self.assertFalse(BusinessCustomerCleanup.objects.exists())

    def test_only_a_manager_looks_merges_or_keeps_apart(self):
        for role in (UserProfile.ROLE_PARTNER, UserProfile.ROLE_WORKER):
            with self.subTest(role=role):
                self.client.force_authenticate(make_user(f'{role}-clean@test', role))
                self.assertEqual(self.client.get(f'{URL}duplicates/').status_code, 403)
                self.assertIn(self.merge(self.mintz, [self.rosh]).status_code, (403, 404))
                self.assertEqual(
                    self.client.post(f'{URL}keep-apart/', {'card_ids': [str(self.mintz.pk), str(self.rosh.pk)]},
                                     format='json').status_code, 403,
                )
        self.assertEqual(BusinessCustomer.objects.count(), 3)
