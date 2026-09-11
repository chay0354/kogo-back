"""What a rental contract says: the terms frozen from a tenancy, their fingerprint, the PDF drawn from them, and a contract that never changes."""
import re
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from reportlab.pdfgen.canvas import Canvas

from apps.core.models import UserProfile
from apps.rentals.contracts import build_terms, issue_contract
from apps.rentals.models import FrozenContractError, RentalContract, Tenancy, sha256_hex
from apps.rentals.tests.factories import (
    make_branch, make_customer, make_rental, make_studio, make_tenancy, make_user, sign_directly,
)
from apps.scheduling.models import ScheduleEvent
from apps.scheduling.rental_agreement import content, generator
from apps.scheduling.rental_agreement.terms import TEMPLATE_VERSION, canonical_json, terms_sha256


def _reversed_keys(value):
    """The same data with every dict's keys written in reverse order."""
    if isinstance(value, dict):
        return {key: _reversed_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reversed_keys(item) for item in value]
    return value


def _page_count(pdf: bytes) -> int:
    return len(re.findall(rb'/Type /Page[^s]', pdf))


def _render_collecting_centred_strings(terms, **kwargs):
    """Render the PDF and return it with every string drawn centred on a page (the footer is one)."""
    drawn = []
    original = Canvas.drawCentredString

    def spy(canvas, x, y, text, *args, **kw):
        drawn.append(text)
        return original(canvas, x, y, text, *args, **kw)

    with mock.patch.object(Canvas, 'drawCentredString', spy):
        pdf = generator.generate_tenancy_contract_pdf(terms, **kwargs)
    return pdf, drawn


class ContractTestCase(TestCase):
    def setUp(self):
        self.branch = make_branch('פלורנטין')
        big = make_studio(self.branch, 'סטודיו גדול')
        small = make_studio(self.branch, 'סטודיו קטן')
        # Agreed at 1234.56 a month before VAT, not what the slots add up to.
        self.tenancy = make_tenancy(self.branch)
        # Sundays at the slot's own hours, Tuesdays at that day's: 100 × 4 each.
        self.weekly = make_rental(
            self.branch, price='100', days=(0, 2), studio=big, tenancy=self.tenancy,
            weekly_day_times={'2': {'start_time': '18:00', 'end_time': '19:30'}},
        )
        # One Thursday, billed once.
        self.once = make_rental(
            self.branch, price='250', event_type='one_time', studio=small, tenancy=self.tenancy,
            event_date=date(2026, 10, 15),
        )
        # Rents nothing any more, so the contract does not list it.
        self.gone = make_rental(self.branch, price='999', days=(4,), studio=big, tenancy=self.tenancy, is_active=False)


class BuildTermsTests(ContractTestCase):
    def test_the_snapshot_states_the_agreed_amount_and_every_active_slot(self):
        terms = build_terms(self.tenancy)

        self.assertEqual(terms['template_version'], TEMPLATE_VERSION)
        self.assertEqual(TEMPLATE_VERSION, '2026-09-v1')
        self.assertEqual(terms['studio'], {
            'name': content.STUDIO_NAME, 'company_number': content.STUDIO_COMPANY_NUMBER,
            'email': content.STUDIO_EMAIL, 'phone': content.STUDIO_PHONE,
        })
        self.assertEqual(terms['tenant'], {
            'name': 'סטודיו אור', 'company_number': '512345678', 'id_number': '',
            'phone': '050-1234567', 'email': 'or@example.com', 'address': 'הרצל 1',
        })
        self.assertEqual(terms['branch'], {'name': 'פלורנטין'})
        self.assertEqual(terms['rows'], [
            {
                'kind': 'weekly', 'weekday': 0, 'date': None, 'start_time': '10:00', 'end_time': '11:00',
                'studio': 'סטודיו גדול', 'rate': '100.00', 'sum': '400.00',
            },
            {
                'kind': 'weekly', 'weekday': 2, 'date': None, 'start_time': '18:00', 'end_time': '19:30',
                'studio': 'סטודיו גדול', 'rate': '100.00', 'sum': '400.00',
            },
            {
                'kind': 'one_time', 'weekday': None, 'date': '2026-10-15', 'start_time': '10:00',
                'end_time': '11:00', 'studio': 'סטודיו קטן', 'rate': '250.00', 'sum': '250.00',
            },
        ])
        # The agreed amount, never the rows added up (400 + 400 + 250).
        self.assertEqual(terms['monthly_amount'], '1234.56')
        self.assertEqual(terms['vat_rate'], '0.18')
        # 1234.56 × 1.18 = 1456.7808: to the agora, not to the shekel.
        self.assertEqual(terms['vat_amount'], '222.22')
        self.assertEqual(terms['monthly_total'], '1456.78')
        self.assertEqual(terms['billing_day'], 10)
        self.assertEqual(terms['start_date'], '2026-09-01')
        self.assertEqual(terms['end_date'], '2027-08-31')

    def test_the_total_is_what_the_tenancy_charges(self):
        self.assertEqual(build_terms(self.tenancy)['monthly_total'], f'{self.tenancy.monthly_total:.2f}')


class FingerprintTests(ContractTestCase):
    def test_canonical_json_is_sorted_compact_and_keeps_the_hebrew(self):
        self.assertEqual(canonical_json({'b': [1, None], 'a': 'אור'}), '{"a":"אור","b":[1,null]}')

    def test_key_order_does_not_change_the_fingerprint(self):
        terms = build_terms(self.tenancy)
        reordered = _reversed_keys(terms)
        self.assertNotEqual(list(reordered), list(terms))
        self.assertEqual(terms_sha256(reordered), terms_sha256(terms))

    def test_a_decimal_hashes_as_its_string(self):
        self.assertEqual(terms_sha256({'amount': Decimal('1234.56')}), terms_sha256({'amount': '1234.56'}))

    def test_any_change_changes_it(self):
        terms = build_terms(self.tenancy)
        self.assertNotEqual(terms_sha256({**terms, 'monthly_amount': '1234.57'}), terms_sha256(terms))

    def test_the_order_the_slots_are_read_in_does_not_change_the_terms(self):
        def read(order):
            return Tenancy.objects.select_related('tenant', 'branch').prefetch_related(
                Prefetch('slots', queryset=ScheduleEvent.objects.select_related('studio').order_by(order)),
            ).get(pk=self.tenancy.pk)

        self.assertEqual(build_terms(read('created_at')), build_terms(read('-created_at')))

    def test_a_stored_contract_rehashes_to_its_fingerprint(self):
        issued = issue_contract(self.tenancy, None)
        stored = RentalContract.objects.get(pk=issued.pk)
        self.assertEqual(terms_sha256(stored.terms), stored.terms_sha256)
        self.assertEqual(stored.terms_sha256, terms_sha256(build_terms(self.tenancy)))
        self.assertEqual(stored.pdf_sha256, sha256_hex(stored.pdf))


class RendererTests(ContractTestCase):
    def test_a_stored_contract_carries_its_version_and_fingerprint_on_every_page(self):
        terms = build_terms(self.tenancy)
        pdf, drawn = _render_collecting_centred_strings(terms, version=3)

        self.assertTrue(pdf.startswith(b'%PDF'))
        footer = generator.contract_footer_text(3, terms)
        self.assertEqual(footer, f'גרסת חוזה 3 · מזהה תנאים {terms_sha256(terms)[:12]}')
        pages = _page_count(pdf)
        self.assertGreaterEqual(pages, 2)
        self.assertEqual(drawn.count(generator._rtl_line(footer)), pages)

    def test_without_a_version_no_footer_is_drawn(self):
        terms = build_terms(self.tenancy)
        pdf, drawn = _render_collecting_centred_strings(terms)
        self.assertTrue(pdf.startswith(b'%PDF'))
        self.assertFalse([text for text in drawn if terms_sha256(terms)[:12] in text])

    def test_the_payment_table_lists_every_row_and_the_agreed_totals_to_the_agora(self):
        generator._ensure_fonts_registered()
        terms = build_terms(self.tenancy)
        table, _space, totals = generator._build_payment_table(terms, generator._build_styles())

        self.assertEqual([row[0].text for row in table._cellvalues[1:]], ['₪400.00', '₪400.00', '₪250.00'])
        self.assertEqual([row[0].text for row in totals._cellvalues], ['₪1234.56', '₪222.22', '₪1456.78'])

    def test_a_name_with_markup_characters_still_renders(self):
        terms = build_terms(self.tenancy)
        terms['tenant'] = {**terms['tenant'], 'name': 'אור & <שותפים>', 'address': 'הרצל 1 <ב>'}
        self.assertTrue(generator.generate_tenancy_contract_pdf(terms, version=1).startswith(b'%PDF'))


class FrozenContractTests(ContractTestCase):
    def setUp(self):
        super().setUp()
        self.issuer = make_user('issuer@test', UserProfile.ROLE_MANAGER)
        self.contract = issue_contract(self.tenancy, self.issuer)

    def fresh(self):
        return RentalContract.objects.get(pk=self.contract.pk)

    def test_what_a_contract_says_never_changes(self):
        changes = {
            'terms': {**self.contract.terms, 'monthly_amount': '1.00'},
            'pdf': b'%PDF-1.4 another file',
            'version': 9,
            'tenancy': make_tenancy(self.branch, tenant=make_customer('יעל', 'בר')),
            'terms_sha256': '0' * 64,
            'pdf_sha256': '0' * 64,
            'created_at': timezone.now(),
            'created_by': make_user('someone-else@test', UserProfile.ROLE_MANAGER),
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                contract = self.fresh()
                setattr(contract, field, value)
                with self.assertRaises(FrozenContractError):
                    contract.save()

        stored = self.fresh()
        self.assertEqual(stored.terms, self.contract.terms)
        self.assertEqual(bytes(stored.pdf), bytes(self.contract.pdf))
        self.assertEqual(
            (stored.version, stored.tenancy_id, stored.created_by_id, stored.created_at),
            (1, self.tenancy.pk, self.issuer.pk, self.contract.created_at),
        )

    def test_only_its_life_moves_on(self):
        contract = self.fresh()
        contract.status = RentalContract.STATUS_SENT
        contract.save()
        contract.status = RentalContract.STATUS_VOID
        contract.voided_at = timezone.now()
        contract.void_reason = 'טעות בסכום'
        contract.save()
        self.assertEqual(self.fresh().void_reason, 'טעות בסכום')

        # Loaded without its heavy columns, as the API loads it.
        light = RentalContract.objects.defer('pdf', 'terms').get(pk=self.contract.pk)
        light.void_reason = 'טעות בשם'
        light.save(update_fields=['void_reason'])
        self.assertEqual(self.fresh().void_reason, 'טעות בשם')

    def test_the_fingerprints_are_derived_never_supplied(self):
        other = RentalContract.objects.create(
            tenancy=self.tenancy, version=2, status=RentalContract.STATUS_VOID,
            terms={'a': 1}, pdf=b'%PDF-x', terms_sha256='f' * 64, pdf_sha256='f' * 64,
        )
        other.refresh_from_db()
        self.assertEqual(other.terms_sha256, terms_sha256({'a': 1}))
        self.assertEqual(other.pdf_sha256, sha256_hex(b'%PDF-x'))

    def test_a_bulk_update_is_refused_the_frozen_fields(self):
        same = RentalContract.objects.filter(pk=self.contract.pk)
        for change in ({'terms': {}}, {'pdf': b''}, {'version': 2}, {'created_by': make_user('x@test', UserProfile.ROLE_MANAGER)}):
            with self.subTest(change=list(change)):
                with self.assertRaises(FrozenContractError):
                    same.update(**change)
        self.assertEqual(same.update(status=RentalContract.STATUS_SENT), 1)
        self.assertEqual(same.update(created_by=None), 1)

    def test_deleting_the_issuers_account_keeps_the_contract(self):
        self.issuer.delete()
        stored = self.fresh()
        self.assertIsNone(stored.created_by_id)
        self.assertEqual(stored.terms_sha256, self.contract.terms_sha256)

    def test_a_tenancy_with_contracts_is_not_deleted(self):
        ScheduleEvent.objects.filter(tenancy=self.tenancy).update(tenancy=None)
        with self.assertRaises(ProtectedError):
            self.tenancy.delete()

    def test_the_database_holds_one_open_and_one_signed_contract_per_tenancy(self):
        extra = {'tenancy': self.tenancy, 'terms': {}, 'pdf': b'%PDF'}
        with self.assertRaises(IntegrityError), transaction.atomic():
            RentalContract.objects.create(version=2, **extra)
        signed = sign_directly(self.contract)
        # Signed with everything a signed contract holds, so only the one-signed rule can refuse it.
        with self.assertRaises(IntegrityError) as refused, transaction.atomic():
            RentalContract.objects.create(
                version=2, status=RentalContract.STATUS_SIGNED, signed_at=signed.signed_at,
                signature=signed.signature, signed_pdf=b'%PDF', signed_pdf_sha256='0' * 64, **extra,
            )
        self.assertIn('rental_contract_one_signed_per_tenancy', str(refused.exception))


class AdminTests(ContractTestCase):
    def test_the_admin_shows_contracts_and_changes_none(self):
        contract = issue_contract(self.tenancy, None)
        self.client.force_login(get_user_model().objects.create_superuser('admin-contracts@test', 'a@test.com', 'pw'))

        listed = self.client.get(reverse('admin:rentals_rentalcontract_changelist'))
        self.assertEqual(listed.status_code, 200)
        page = self.client.get(reverse('admin:rentals_rentalcontract_change', args=[contract.pk]))
        self.assertContains(page, contract.terms_sha256)
        self.assertContains(page, contract.pdf_sha256)
        self.assertNotContains(page, 'name="_save"')
        self.assertEqual(self.client.get(reverse('admin:rentals_rentalcontract_add')).status_code, 403)
