"""The whole history in one run: open invoices set aside, a shared company
number split back into its customers, and a file too big for one request.

Every name and number is invented."""
import gzip
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from rest_framework.test import APITestCase

from apps.customers.models import BusinessCustomer
from apps.legacy_import import service
from apps.legacy_import.models import LegacyDocument, LegacyImport
from apps.legacy_import.parser import customers_from_rows, prepare_rows, type_table
from apps.legacy_import.tests.helpers import CORRUPT_XLS, row
from apps.legacy_import.tests.test_api import BASE, ImportFixture

NETWORK = '580000001'  # a company number three community centres invoice under


def centre(ext_number, name, **overrides):
    values = {'id_number': NETWORK, 'ext_number': ext_number, 'doc_type': 'tax_invoice',
              'first_name': 'רשת', 'last_name': name, 'payment_type': ''}
    values.update(overrides)
    return row(**values)


def prepared(rows):
    prepare_rows(rows)
    return rows


class OpenInvoiceTests(SimpleTestCase):
    def test_only_an_invoice_can_be_open(self):
        rows = prepared([
            row(doc_type='tax_invoice', status='פתוחה'),
            row(doc_type='transaction_invoice', status='פתוחה'),
            row(doc_type='tax_invoice', status='סגורה'),
            row(doc_type='tax_invoice', status='מזוכת חלקית'),
            row(doc_type='receipt', status='פתוחה'),
            row(doc_type='combined', status='פתוחה'),
        ])
        self.assertEqual([bool(r.get('open')) for r in rows], [True, True, False, False, False, False])

    def test_marking_twice_is_the_same_and_a_closed_invoice_is_unmarked(self):
        rows = prepared([row(doc_type='tax_invoice', status='פתוחה')])
        rows[0]['status'] = 'סגורה'
        self.assertEqual(prepare_rows(rows)['open_invoices'], 0)
        self.assertNotIn('open', rows[0])

    def test_an_open_invoice_gives_the_details_and_is_not_counted(self):
        customers = customers_from_rows(prepared([
            row(ext_number='5', doc_type='tax_invoice', number=10, date='2025-01-01', location='כפר סבא'),
            row(ext_number='5', doc_type='tax_invoice', number=11, date='2025-06-01', status='פתוחה',
                email='new@example.test', location=''),
        ]))
        customer = customers['ext:5']
        self.assertEqual((customer.documents, customer.types), (1, {'tax_invoice': 1}))
        self.assertEqual(customer.latest['number'], 10)
        self.assertEqual(customer.email, 'new@example.test')

    def test_a_customer_with_nothing_but_an_open_invoice_is_still_a_business_customer(self):
        customers = customers_from_rows(prepared([
            row(ext_number='6', doc_type='tax_invoice', number=12, status='פתוחה', location='רמת גן'),
        ]))
        customer = customers['ext:6']
        self.assertEqual((customer.kind, customer.documents, customer.types), ('business', 0, {}))
        self.assertEqual(customer.latest['location'], 'רמת גן')

    def test_the_numbering_table_still_ends_at_the_open_invoice(self):
        table = type_table(prepared([
            row(doc_type='tax_invoice', number=40001, date='2025-01-01'),
            row(doc_type='tax_invoice', number=40002, date='2025-02-01', status='פתוחה'),
        ]))
        self.assertEqual((table[0]['last_number'], table[0]['count'], table[0]['open']), (40002, 2, 1))


class LatestLocationTests(SimpleTestCase):
    def test_a_receipt_without_a_location_does_not_unfile_the_customer(self):
        customers = customers_from_rows([
            row(ext_number='7', doc_type='tax_invoice', date='2025-01-01', location='כפר סבא'),
            row(ext_number='7', doc_type='receipt', date='2025-02-01', location=''),
        ])
        self.assertEqual(customers['ext:7'].latest['location'], 'כפר סבא')
        self.assertEqual(customers['ext:7'].latest['doc_type'], 'receipt')


class SharedCompanyNumberTests(SimpleTestCase):
    def test_a_company_number_under_several_names_is_several_customers(self):
        rows = prepared([centre('21', 'מתנ"ס צפון'), centre('24', 'מתנ"ס דרום'), centre('62', 'מתנ"ס מזרח')])
        customers = customers_from_rows(rows)
        self.assertEqual(sorted(customers), [f'{NETWORK}/ext:21', f'{NETWORK}/ext:24', f'{NETWORK}/ext:62'])
        north = customers[f'{NETWORK}/ext:21']
        self.assertTrue(north.shares_id)
        self.assertEqual((north.full_name, north.company_number, north.names), ('רשת מתנ"ס צפון', NETWORK, ['רשת מתנ"ס צפון']))

    def test_the_same_name_under_two_customer_numbers_is_one_customer(self):
        customers = customers_from_rows(prepared([centre('21', 'מתנ"ס צפון'), centre('22', 'מתנ״ס  צפון')]))
        self.assertEqual(list(customers), [NETWORK])
        self.assertFalse(customers[NETWORK].shares_id)

    def test_a_person_under_two_names_is_one_customer_who_changed_name(self):
        customers = customers_from_rows(prepared([
            row(id_number='203000001', ext_number='33', doc_type='tax_invoice', date='2025-03-01',
                first_name='נועם', last_name='מדריך'),
            row(id_number='203000001', ext_number='18', doc_type='tax_invoice', date='2026-01-01',
                first_name='נועם', last_name='תנועה'),
        ]))
        self.assertEqual(list(customers), ['203000001'])
        self.assertEqual(customers['203000001'].names, ['נועם מדריך', 'נועם תנועה'])

    def test_one_customer_number_that_changed_its_name_is_not_split(self):
        customers = customers_from_rows(prepared([
            centre('21', 'מתנ"ס ישן', date='2025-01-01'), centre('21', 'מתנ"ס חדש', date='2025-06-01'),
        ]))
        self.assertEqual(list(customers), [NETWORK])

    def test_preparing_twice_changes_nothing(self):
        rows = prepared([centre('21', 'מתנ"ס צפון'), centre('24', 'מתנ"ס דרום')])
        keys = [r['customer_key'] for r in rows]
        self.assertEqual(prepare_rows(rows)['customers_sharing_an_id'], 2)
        self.assertEqual([r['customer_key'] for r in rows], keys)


class HistoryRunCommitTests(ImportFixture, APITestCase):
    def test_an_open_invoice_is_listed_and_not_written(self):
        rows = prepared([
            row(ext_number='5', doc_type='tax_invoice', number=40001, date='2025-01-01'),
            row(ext_number='5', doc_type='tax_invoice', number=40002, date='2025-06-01', status='פתוחה',
                invoice_total='1180.00', details='הדרכות יוני', first_name='להקת', last_name='הדגמה'),
        ])
        summary = service.build_summary(rows, [])
        self.assertEqual((summary['documents']['total'], summary['documents']['open_invoices']), (1, 1))
        listed = summary['open_invoices']
        self.assertEqual((listed['count'], listed['total']), (1, '1180.00'))
        self.assertEqual(
            (listed['rows'][0]['number'], listed['rows'][0]['customer_name'], listed['rows'][0]['details']),
            (40002, 'להקת הדגמה', 'הדרכות יוני'),
        )

        result = self.commit(self.stored_import(rows)).data

        self.assertEqual((result['documents']['created'], result['documents']['open_skipped']), (1, 1))
        self.assertEqual(list(LegacyDocument.objects.values_list('number', flat=True)), [40001])
        # The card's note counts what was imported, and its newest document is the closed one.
        note = BusinessCustomer.objects.get().notes
        self.assertIn('1 מסמכים', note)
        self.assertIn('40001', note)
        self.assertNotIn('40002', note)

    def test_a_customer_with_only_an_open_invoice_gets_a_card_and_no_history(self):
        rows = prepared([row(ext_number='6', doc_type='tax_invoice', number=40003, status='פתוחה',
                             first_name='מרכז', last_name='הדגמה', email='centre@example.test')])
        result = self.commit(self.stored_import(rows)).data
        self.assertEqual((result['customers']['created'], result['documents']['created']), (1, 0))
        card = BusinessCustomer.objects.get()
        self.assertEqual(card.email, 'centre@example.test')
        self.assertIn('החשבונית הפתוחה שלו נשארה בתוכנה הקודמת', card.notes)
        self.assertEqual(LegacyDocument.objects.count(), 0)

    def test_an_invoice_that_was_open_is_imported_once_it_closes(self):
        rows = prepared([row(ext_number='6', doc_type='tax_invoice', number=40003, status='פתוחה')])
        self.commit(self.stored_import(rows))
        later = prepared([row(ext_number='6', doc_type='tax_invoice', number=40003, status='סגורה')])
        result = self.commit(self.stored_import(later)).data
        self.assertEqual((result['documents']['created'], result['customers']['created']), (1, 0))
        self.assertEqual(LegacyDocument.objects.get().business_customer, BusinessCustomer.objects.get())

    def test_each_centre_of_a_network_gets_its_own_card(self):
        rows = prepared([
            centre('21', 'מתנ"ס צפון', number=1, email='office@example.test'),
            centre('21', 'מתנ"ס צפון', number=2, email='office@example.test'),
            centre('24', 'מתנ"ס דרום', number=3, email='office@example.test'),
        ])
        legacy_import = self.stored_import(rows)
        summary = service.build_summary(rows, [])
        self.assertEqual(summary['customers']['business_create'], 2)
        self.assertEqual(summary['name_changes'], [])
        self.assertEqual(
            [(c['ext_number'], c['name'], c['documents']) for c in summary['shared_ids'][0]['customers']],
            [('24', 'רשת מתנ"ס דרום', 1), ('21', 'רשת מתנ"ס צפון', 2)],
        )

        result = self.commit(legacy_import).data

        self.assertEqual(result['customers']['created'], 2)
        north = BusinessCustomer.objects.get(last_name='מתנ"ס צפון')
        south = BusinessCustomer.objects.get(last_name='מתנ"ס דרום')
        self.assertEqual((north.company_number, south.company_number), (NETWORK, NETWORK))
        self.assertEqual(sorted(north.legacy_documents.values_list('number', flat=True)), [1, 2])
        self.assertEqual(list(south.legacy_documents.values_list('number', flat=True)), [3])

        again = self.commit(legacy_import).data
        self.assertEqual((again['customers']['created'], again['customers']['updated'],
                          again['documents']['created'], again['documents']['updated']), (0, 0, 0, 0))

    def test_a_centre_is_matched_to_its_own_card_and_never_to_its_sisters(self):
        sister = BusinessCustomer.objects.create(first_name='רשת', last_name='מתנ"ס מערב', company_number=NETWORK,
                                                 email='office@example.test')
        own = BusinessCustomer.objects.create(first_name='רשת', last_name='מתנ״ס  דרום', company_number='58-000000-1')
        rows = prepared([
            centre('21', 'מתנ"ס צפון', number=1, email='office@example.test'),
            centre('24', 'מתנ"ס דרום', number=3),
        ])
        summary = service.build_summary(rows, [])
        self.assertEqual((summary['customers']['business_create'], summary['customers']['business_update']), (1, 1))
        matched = next(c for c in summary['customers']['business_list'] if c['action'] == 'update')
        self.assertEqual(matched['match']['how'], 'לפי ח"פ ושם')

        self.commit(self.stored_import(rows))

        self.assertEqual(LegacyDocument.objects.get(number=3).business_customer, own)
        north = LegacyDocument.objects.get(number=1).business_customer
        self.assertNotIn(north, (sister, own))
        self.assertEqual(sister.legacy_documents.count(), 0)

    def test_a_number_that_becomes_shared_in_a_later_export_keeps_its_first_card(self):
        first = prepared([centre('21', 'מתנ"ס צפון', number=1)])
        self.commit(self.stored_import(first))
        card = BusinessCustomer.objects.get()
        self.assertEqual(LegacyDocument.objects.get(number=1).customer_key, NETWORK)

        later = prepared([centre('21', 'מתנ"ס צפון', number=1), centre('24', 'מתנ"ס דרום', number=2)])
        result = self.commit(self.stored_import(later)).data

        self.assertEqual(result['customers']['created'], 1)
        moved = LegacyDocument.objects.get(number=1)
        self.assertEqual((moved.customer_key, moved.business_customer), (f'{NETWORK}/ext:21', card))
        self.assertNotEqual(LegacyDocument.objects.get(number=2).business_customer, card)

    def test_a_person_who_changed_name_is_one_card_with_the_new_name(self):
        rows = prepared([
            row(id_number='203000001', ext_number='33', doc_type='tax_invoice', number=1, date='2025-03-01',
                first_name='נועם', last_name='מדריך'),
            row(id_number='203000001', ext_number='18', doc_type='tax_invoice', number=2, date='2026-01-01',
                first_name='נועם', last_name='תנועה'),
        ])
        self.commit(self.stored_import(rows))
        card = BusinessCustomer.objects.get()
        self.assertEqual((card.first_name, card.last_name, card.id_number), ('נועם', 'תנועה', '203000001'))
        self.assertEqual(card.legacy_documents.count(), 2)

    def test_searching_a_shared_company_number_finds_every_centre(self):
        self.commit(self.stored_import(prepared([
            centre('21', 'מתנ"ס צפון', number=1), centre('24', 'מתנ"ס דרום', number=3),
        ])))
        found = self.client.get(f'{BASE}documents/', {'q': NETWORK}).data['results']
        self.assertEqual(sorted(d['number'] for d in found), [1, 3])

    def test_the_last_number_is_the_open_invoices_when_it_is_the_newest(self):
        rows = prepared([
            row(ext_number='5', doc_type='tax_invoice', number=40001, date='2025-01-01'),
            row(ext_number='5', doc_type='tax_invoice', number=40002, date='2025-06-01', status='פתוחה'),
            row(ext_number='5', doc_type='receipt', number=33001, date='2025-02-01'),
        ])
        legacy_import = LegacyImport.objects.create(
            file_name='t.xls', sha256='1' * 64, row_count=2, rows=rows, summary=service.build_summary(rows, []),
        )
        self.commit(legacy_import)
        series = {s['doc_type']: s for s in self.client.get(f'{BASE}series/').data['series']}
        tax = series['tax_invoice']
        self.assertEqual((tax['last_number'], tax['last_date'], tax['last_not_imported'], tax['count']),
                         (40002, '2025-06-01', True, 1))
        self.assertEqual((series['receipt']['last_number'], series['receipt']['last_not_imported']), (33001, False))


class CardsOnlyTests(ImportFixture, APITestCase):
    """The business starts its own numbering in kogo: the customers come over, the documents stay behind."""

    def rows(self):
        return prepared([
            centre('21', 'מתנ"ס צפון', number=1, date='2025-01-01', location='כפר סבא', email='north@example.test'),
            centre('21', 'מתנ"ס צפון', number=2, date='2025-06-01', location=''),
            centre('24', 'מתנ"ס דרום', number=3, date='2025-03-01', status='פתוחה'),
            # A tenant with nothing to find them by but the old software's customer number.
            row(ext_number='77', number=4, details='השכרת סטודיו', remark='תשלום בהוראת קבע #9',
                first_name='שוכרת', last_name='הדגמה'),
            # A parent: no card, cards-only or not.
            row(ext_number='88', number=5, first_name='הורה', last_name='בדיקה'),
        ])

    def cards_only(self, legacy_import, mapping=None, **extra):
        return self.client.post(
            f'{BASE}{legacy_import.pk}/commit/',
            {'mapping': mapping or {}, 'import_documents': False, **extra}, format='json',
        )

    def test_the_cards_are_opened_and_not_one_document_is_kept(self):
        legacy_import = self.stored_import(self.rows())

        res = self.cards_only(legacy_import, {'כפר סבא': self.branch_target(self.kfar_saba)})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['customers']['created'], 3)
        self.assertEqual(
            (res.data['documents']['imported'], res.data['documents']['created'], res.data['documents']['total'],
             res.data['documents']['left_in_previous_software']),
            (False, 0, 0, 5),
        )
        self.assertEqual(LegacyDocument.objects.count(), 0)
        north = BusinessCustomer.objects.get(last_name='מתנ"ס צפון')
        self.assertEqual((north.company_number, north.email), (NETWORK, 'north@example.test'))
        # Filed where the last document that named a location was.
        self.assertEqual((north.business, north.business_category, north.branch),
                         (self.lessons, self.branches_category, self.kfar_saba))
        self.assertIn('פרטי הלקוח בלבד, בלי המסמכים', north.notes)
        self.assertIn('הופקו לו 2 מסמכים', north.notes)
        # A customer whose only document is an open invoice gets a card too.
        self.assertTrue(BusinessCustomer.objects.filter(last_name='מתנ"ס דרום').exists())
        self.assertFalse(BusinessCustomer.objects.filter(last_name='בדיקה').exists())

    def test_the_rows_are_dropped_and_the_same_import_cannot_be_run_again(self):
        legacy_import = self.stored_import(self.rows())
        self.cards_only(legacy_import)
        legacy_import.refresh_from_db()
        self.assertEqual((legacy_import.rows, legacy_import.status), ([], LegacyImport.STATUS_COMMITTED))

        again = self.cards_only(legacy_import)
        self.assertEqual(again.status_code, 400)
        self.assertIn('העלו את הקובץ מחדש', again.data['error'])
        self.assertEqual(BusinessCustomer.objects.count(), 3)

    def test_the_same_file_uploaded_again_finds_every_card_it_opened(self):
        self.cards_only(self.stored_import(self.rows()))
        cards = set(BusinessCustomer.objects.values_list('pk', flat=True))

        res = self.cards_only(self.stored_import(self.rows()))

        self.assertEqual((res.data['customers']['created'], res.data['customers']['updated']), (0, 0))
        self.assertEqual(set(BusinessCustomer.objects.values_list('pk', flat=True)), cards)

    def test_a_later_import_with_documents_links_them_to_the_cards_already_there(self):
        self.cards_only(self.stored_import(self.rows()))
        tenant = BusinessCustomer.objects.get(first_name='שוכרת')

        res = self.commit(self.stored_import(self.rows()))

        self.assertEqual((res.data['customers']['created'], res.data['documents']['created']), (0, 4))
        self.assertEqual(LegacyDocument.objects.get(number=4).business_customer, tenant)
        self.assertEqual(BusinessCustomer.objects.count(), 3)

    def test_an_import_with_documents_still_keeps_its_rows(self):
        legacy_import = self.stored_import(self.rows())
        self.commit(legacy_import)
        legacy_import.refresh_from_db()
        self.assertEqual(len(legacy_import.rows), 5)
        self.assertTrue(legacy_import.result['documents']['imported'])

    def test_importing_nothing_is_refused(self):
        legacy_import = self.stored_import(self.rows())
        res = self.cards_only(legacy_import, create_customers=False)
        self.assertEqual(res.status_code, 400)
        self.assertIn('לא נבחר מה לייבא', res.data['error'])
        self.assertEqual((BusinessCustomer.objects.count(), LegacyDocument.objects.count()), (0, 0))

    def test_cards_only_leaves_the_numbering_screen_nothing_to_continue(self):
        legacy_import = LegacyImport.objects.create(
            file_name='t.xls', sha256='2' * 64, row_count=5, rows=self.rows(),
            summary=service.build_summary(self.rows(), []),
        )
        self.cards_only(legacy_import)
        self.assertEqual(self.client.get(f'{BASE}series/').data['series'], [])


class PackedUploadTests(ImportFixture, APITestCase):
    def packed(self, content=None, name='export_invoices.xls.gz'):
        return SimpleUploadedFile(name, gzip.compress(content or CORRUPT_XLS.read_bytes()),
                                  content_type='application/gzip')

    def test_a_gzipped_export_is_the_same_import_as_the_file_itself(self):
        plain = self.client.post(
            f'{BASE}preview/',
            {'file': SimpleUploadedFile('export_invoices.xls', CORRUPT_XLS.read_bytes())},
        ).data
        res = self.client.post(f'{BASE}preview/', {'file': self.packed()})
        self.assertEqual(res.status_code, 201)
        self.assertEqual((res.data['id'], res.data['file_name']), (plain['id'], 'export_invoices.xls'))
        self.assertEqual(res.data['summary']['documents'], plain['summary']['documents'])

    def test_a_file_bigger_than_one_request_goes_through_packed(self):
        with mock.patch.object(service, 'MAX_UPLOAD_BYTES', len(CORRUPT_XLS.read_bytes()) - 1):
            refused = self.client.post(
                f'{BASE}preview/', {'file': SimpleUploadedFile('export_invoices.xls', CORRUPT_XLS.read_bytes())},
            )
            packed = self.client.post(f'{BASE}preview/', {'file': self.packed()})
        self.assertEqual((refused.status_code, packed.status_code), (400, 201))

    def test_what_it_unpacks_to_is_bounded(self):
        with mock.patch.object(service, 'MAX_UNPACKED_BYTES', 1000):
            res = self.client.post(f'{BASE}preview/', {'file': self.packed(b'\0' * 5000)})
        self.assertEqual(res.status_code, 400)
        self.assertIn('גדול מדי', res.data['error'])
        self.assertEqual(LegacyImport.objects.count(), 0)

    def test_a_broken_or_cut_gzip_is_refused_in_words(self):
        whole = gzip.compress(CORRUPT_XLS.read_bytes())
        for name, content in (('cut', whole[:len(whole) // 2]), ('broken', whole[:10] + b'\xff' * 40)):
            with self.subTest(name):
                res = self.client.post(
                    f'{BASE}preview/', {'file': SimpleUploadedFile('export_invoices.xls.gz', content)},
                )
                self.assertEqual(res.status_code, 400)
                self.assertIn('העלו את הקובץ שוב', res.data['error'])
