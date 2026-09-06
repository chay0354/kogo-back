"""
The period report used to describe a month by its documents alone, which made
a month look smaller than it was: a lesson charge issues no document, and a
store order whose Tranzila call failed leaves none either. These tests hold the
extra section to the same rules as the documents — the right rows, the right
sum, and a partner still seeing only their own branches.
"""
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Branch, City, UserProfile
from apps.customers.financial_models import Invoice
from apps.customers.models import Child, Family, Payment
from apps.documents.tests.test_period_report import make_user
from apps.documents.undocumented_income import (
    SOURCE_LESSONS,
    SOURCE_ORPHAN_CHARGES,
    SOURCE_STORE,
    collect_undocumented,
)
from apps.documents.models import FormalDocument
from apps.store.models import StoreInvoice

AUG = (date(2026, 8, 1), date(2026, 8, 31))


def at(day, hour=12):
    return datetime(2026, 8, day, hour, tzinfo=dt_timezone.utc)


def make_lesson_invoice(number, family, branch, day, amount, status='paid'):
    return Invoice.objects.create(
        invoice_number=number,
        family=family,
        branch=branch,
        amount=Decimal(amount),
        status=status,
        payment_method='credit_card',
        payment_type='recurring',
        payer_name=family.name,
        invoice_date=at(day),
    )


def make_store_invoice(number, branch, day, amount, *, formal=None, payment_status='completed'):
    invoice = StoreInvoice.objects.create(
        invoice_number=number,
        branch=branch,
        customer_name='לקוח חנות',
        total_amount=Decimal(amount),
        payment_method='cash',
        payment_status=payment_status,
        formal_document=formal,
    )
    # issue_date is auto_now_add, so the row has to be dated after the fact.
    StoreInvoice.objects.filter(pk=invoice.pk).update(issue_date=at(day))
    return StoreInvoice.objects.get(pk=invoice.pk)


class UndocumentedIncomeTests(TestCase):
    def setUp(self):
        self.city = City.objects.create(name='עיר בדיקה')
        self.north = Branch.objects.create(name='סניף צפון', city=self.city)
        self.south = Branch.objects.create(name='סניף דרום', city=self.city)
        self.fam_n = Family.objects.create(name='משפחה צפון', branch=self.north)
        self.fam_s = Family.objects.create(name='משפחה דרום', branch=self.south)
        self.manager = make_user('manager-undoc@test', role=UserProfile.ROLE_MANAGER)
        self.manager = type(self.manager).objects.get(pk=self.manager.pk)

    def test_paid_lesson_charges_are_collected(self):
        make_lesson_invoice('INV-1', self.fam_n, self.north, 5, '300.00')
        make_lesson_invoice('INV-2', self.fam_s, self.south, 9, '450.00')

        result = collect_undocumented(self.manager, *AUG)

        self.assertEqual(result.count, 2)
        self.assertEqual(result.total, Decimal('750.00'))
        self.assertEqual([s.source for s in result.sections], [SOURCE_LESSONS])

    def test_unpaid_and_out_of_period_charges_are_left_out(self):
        make_lesson_invoice('INV-PAID', self.fam_n, self.north, 5, '300.00')
        make_lesson_invoice('INV-FAILED', self.fam_n, self.north, 6, '999.00', status='failed')
        outside = make_lesson_invoice('INV-SEPT', self.fam_n, self.north, 5, '888.00')
        Invoice.objects.filter(pk=outside.pk).update(
            invoice_date=datetime(2026, 9, 5, 12, tzinfo=dt_timezone.utc),
        )

        result = collect_undocumented(self.manager, *AUG)

        self.assertEqual(result.count, 1)
        self.assertEqual(result.total, Decimal('300.00'))

    def test_store_sale_with_a_document_is_not_reported_twice(self):
        documented = FormalDocument.objects.create(
            document_number='2026-9001',
            document_type='combined',
            client_type='existing',
            branch=self.north,
            document_date=date(2026, 8, 4),
            subtotal=Decimal('100.00'),
            total_amount=Decimal('100.00'),
        )
        make_store_invoice('ST-DOC', self.north, 4, '100.00', formal=documented)
        make_store_invoice('ST-NODOC', self.north, 5, '80.00')

        result = collect_undocumented(self.manager, *AUG)

        self.assertEqual([s.source for s in result.sections], [SOURCE_STORE])
        self.assertEqual(result.count, 1)
        self.assertEqual(result.total, Decimal('80.00'))
        self.assertEqual(result.sections[0].rows[0].reference, 'ST-NODOC')

    def test_pending_store_order_from_the_website_counts(self):
        invoice = make_store_invoice('ST-WEB', None, 6, '120.00', payment_status='pending')
        StoreInvoice.objects.filter(pk=invoice.pk).update(website_order_number='W-77')
        make_store_invoice('ST-PENDING', self.north, 7, '500.00', payment_status='pending')

        result = collect_undocumented(self.manager, *AUG)

        self.assertEqual(result.count, 1)
        self.assertEqual(result.total, Decimal('120.00'))

    def test_partner_sees_only_their_branch(self):
        make_lesson_invoice('INV-N', self.fam_n, self.north, 5, '300.00')
        make_lesson_invoice('INV-S', self.fam_s, self.south, 6, '450.00')
        make_store_invoice('ST-N', self.north, 7, '80.00')
        make_store_invoice('ST-S', self.south, 8, '90.00')

        partner = make_user('partner-undoc@test', role=UserProfile.ROLE_PARTNER)
        partner.profile.assigned_branches.add(self.north)
        partner = type(partner).objects.get(pk=partner.pk)

        result = collect_undocumented(partner, *AUG)

        self.assertEqual(result.total, Decimal('380.00'))
        references = {row.reference for section in result.sections for row in section.rows}
        self.assertEqual(references, {'INV-N', 'ST-N'})

    def test_partner_without_a_branch_sees_nothing(self):
        make_lesson_invoice('INV-N', self.fam_n, self.north, 5, '300.00')
        partner = make_user('partner-nobranch@test', role=UserProfile.ROLE_PARTNER)
        partner = type(partner).objects.get(pk=partner.pk)

        self.assertTrue(collect_undocumented(partner, *AUG).is_empty)

    def test_charge_without_a_branch_falls_back_to_the_family(self):
        make_lesson_invoice('INV-NOBRANCH', self.fam_n, None, 5, '300.00')

        result = collect_undocumented(self.manager, *AUG)

        self.assertEqual(result.sections[0].rows[0].branch_name, 'סניף צפון')

    def test_child_name_is_used_when_the_charge_names_children(self):
        invoice = make_lesson_invoice('INV-KID', self.fam_n, self.north, 5, '300.00')
        child = Child.objects.create(
            family=self.fam_n, first_name='נועה', last_name='צפוני',
            birth_date=date(2015, 5, 5), gender='female', status='active',
        )
        invoice.children.create(child=child)

        result = collect_undocumented(self.manager, *AUG)

        self.assertEqual(result.sections[0].rows[0].customer, 'נועה צפוני')


class OrphanChargeTests(TestCase):
    """
    A charge whose Invoice row was never written still took the customer's
    money. The report has to say so, and must not say it twice for a charge
    that did get an invoice.
    """

    def setUp(self):
        self.city = City.objects.create(name='עיר בדיקה')
        self.north = Branch.objects.create(name='סניף צפון', city=self.city)
        self.fam = Family.objects.create(name='משפחה צפון', branch=self.north)
        self.kid = Child.objects.create(
            family=self.fam, first_name='איתי', last_name='צפוני',
            birth_date=date(2016, 2, 2), gender='male', status='active',
        )
        self.manager = make_user('manager-orphan@test', role=UserProfile.ROLE_MANAGER)
        self.manager = type(self.manager).objects.get(pk=self.manager.pk)

    def make_payment(self, day, amount, status='completed'):
        return Payment.objects.create(
            child=self.kid,
            family=self.fam,
            branch=self.north,
            payment_type='recurring_subscription',
            status=status,
            base_amount=Decimal(amount),
            final_amount=Decimal(amount),
            payment_date=at(day),
        )

    def test_completed_charge_without_an_invoice_is_reported(self):
        self.make_payment(5, '275.00')

        result = collect_undocumented(self.manager, *AUG)

        self.assertEqual([s.source for s in result.sections], [SOURCE_ORPHAN_CHARGES])
        self.assertEqual(result.total, Decimal('275.00'))

    def test_charge_that_has_an_invoice_is_counted_once(self):
        payment = self.make_payment(5, '275.00')
        invoice = make_lesson_invoice('INV-LINKED', self.fam, self.north, 5, '275.00')
        Invoice.objects.filter(pk=invoice.pk).update(payment=payment)

        result = collect_undocumented(self.manager, *AUG)

        self.assertEqual([s.source for s in result.sections], [SOURCE_LESSONS])
        self.assertEqual(result.total, Decimal('275.00'))

    def test_failed_and_zero_charges_are_left_out(self):
        self.make_payment(5, '275.00', status='failed')
        self.make_payment(6, '0.00')

        self.assertTrue(collect_undocumented(self.manager, *AUG).is_empty)

    def test_undated_charge_falls_back_to_when_it_was_created(self):
        payment = self.make_payment(5, '275.00')
        Payment.objects.filter(pk=payment.pk).update(payment_date=None, created_at=at(7))

        result = collect_undocumented(self.manager, *AUG)

        self.assertEqual(result.total, Decimal('275.00'))
        self.assertEqual(result.sections[0].rows[0].row_date, date(2026, 8, 7))
