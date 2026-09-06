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


class MergeAgainstDocumentsTests(TestCase):
    """
    Until a charge issues its own document, the only way to spot a receipt that
    was raised by hand for that same charge is the customer, the sum and the
    period. These hold that guess to a narrow one: it may not eat a charge it
    cannot account for, and never twice with one document.
    """

    def setUp(self):
        self.city = City.objects.create(name='עיר בדיקה')
        self.north = Branch.objects.create(name='סניף צפון', city=self.city)
        self.fam = Family.objects.create(name='משפחה צפון', branch=self.north)
        self.kid = Child.objects.create(
            family=self.fam, first_name='נועה', last_name='צפוני',
            birth_date=date(2015, 5, 5), gender='female', status='active',
        )
        self.manager = make_user('manager-merge@test', role=UserProfile.ROLE_MANAGER)
        self.manager = type(self.manager).objects.get(pk=self.manager.pk)

    def charge(self, number, amount, day=5):
        invoice = make_lesson_invoice(number, self.fam, self.north, day, amount)
        invoice.children.create(child=self.kid)
        return invoice

    def document(self, number, amount, day=6, doc_type='receipt'):
        return FormalDocument.objects.create(
            document_number=number,
            document_type=doc_type,
            client_type='existing',
            child=self.kid,
            branch=self.north,
            document_date=date(2026, 8, day),
            subtotal=Decimal(amount),
            total_amount=Decimal(amount),
        )

    def test_a_receipt_for_the_same_child_and_sum_absorbs_the_charge(self):
        self.charge('INV-DUP', '320.00')
        self.document('2026-0100', '320.00')

        result = collect_undocumented(self.manager, *AUG)

        self.assertEqual(result.total, Decimal('0.00'))
        self.assertEqual(result.merged_count, 1)
        self.assertEqual(result.sections[0].rows[0].merged_document, '2026-0100')

    def test_one_document_absorbs_only_one_of_two_equal_charges(self):
        self.charge('INV-A', '320.00')
        self.charge('INV-B', '320.00', day=20)
        self.document('2026-0100', '320.00')

        result = collect_undocumented(self.manager, *AUG)

        self.assertEqual(result.merged_count, 1)
        self.assertEqual(result.total, Decimal('320.00'))

    def test_a_different_sum_is_not_merged(self):
        self.charge('INV-DUP', '320.00')
        self.document('2026-0100', '450.00')

        result = collect_undocumented(self.manager, *AUG)

        self.assertEqual(result.merged_count, 0)
        self.assertEqual(result.total, Decimal('320.00'))

    def test_a_credit_note_never_absorbs_a_charge(self):
        self.charge('INV-DUP', '320.00')
        self.document('2026-0100', '320.00', doc_type='credit_invoice')

        result = collect_undocumented(self.manager, *AUG)

        self.assertEqual(result.merged_count, 0)
        self.assertEqual(result.total, Decimal('320.00'))

    def test_a_store_document_never_absorbs_a_lesson_charge(self):
        self.charge('INV-DUP', '320.00')
        store_doc = self.document('2026-0100', '320.00', doc_type='combined')
        make_store_invoice('ST-LINKED', self.north, 6, '320.00', formal=store_doc)

        result = collect_undocumented(self.manager, *AUG)

        self.assertEqual(result.merged_count, 0)
        self.assertEqual(result.total, Decimal('320.00'))


class GroupingTests(TestCase):
    """
    The owner reads the report branch by branch, so the charges have to file
    under the same buckets the documents do — and under a business when the
    report is grouped that way, read off the course the charge was for.
    """

    def setUp(self):
        from apps.core.models import Business, BusinessCategory
        from apps.courses.models import Course

        self.city = City.objects.create(name='עיר בדיקה')
        self.north = Branch.objects.create(name='סניף צפון', city=self.city)
        self.south = Branch.objects.create(name='סניף דרום', city=self.city)
        self.fam_n = Family.objects.create(name='משפחה צפון', branch=self.north)
        self.fam_s = Family.objects.create(name='משפחה דרום', branch=self.south)
        self.kid_n = Child.objects.create(
            family=self.fam_n, first_name='נועה', last_name='צפוני',
            birth_date=date(2015, 5, 5), gender='female', status='active',
        )
        self.gaga = Business.objects.create(name='גאגא')
        self.dance = BusinessCategory.objects.create(business=self.gaga, name='ריקוד')
        self.course = Course.objects.create(
            name='היפ הופ', branch=self.north, price=Decimal('300.00'), capacity=20,
            business=self.gaga, business_category=self.dance,
        )
        self.manager = make_user('manager-group@test', role=UserProfile.ROLE_MANAGER)
        self.manager = type(self.manager).objects.get(pk=self.manager.pk)

    def test_rows_file_under_their_branch_with_a_subtotal_each(self):
        make_lesson_invoice('INV-N1', self.fam_n, self.north, 5, '300.00')
        make_lesson_invoice('INV-N2', self.fam_n, self.north, 9, '200.00')
        make_lesson_invoice('INV-S1', self.fam_s, self.south, 6, '450.00')
        make_store_invoice('ST-N', self.north, 7, '80.00')

        groups = collect_undocumented(self.manager, *AUG).grouped('branch')

        self.assertEqual([g.title for g in groups], ['סניף דרום', 'סניף צפון'])
        north = next(g for g in groups if g.title == 'סניף צפון')
        self.assertEqual(north.total, Decimal('580.00'))
        self.assertEqual([s.source for s in north.sections], [SOURCE_LESSONS, SOURCE_STORE])
        self.assertEqual(north.sections[0].total, Decimal('500.00'))

    def test_a_row_without_a_branch_lands_in_the_catch_all_last(self):
        make_lesson_invoice('INV-N1', self.fam_n, self.north, 5, '300.00')
        make_store_invoice('ST-WEB', None, 6, '120.00')

        groups = collect_undocumented(self.manager, *AUG).grouped('branch')

        self.assertEqual(groups[-1].title, 'ללא שיוך לסניף')
        self.assertTrue(groups[-1].is_unassigned)

    def test_grouping_by_business_reads_the_tag_off_the_course(self):
        tagged = make_lesson_invoice('INV-TAGGED', self.fam_n, self.north, 5, '300.00')
        tagged.children.create(child=self.kid_n, course=self.course)
        make_lesson_invoice('INV-PLAIN', self.fam_s, self.south, 6, '450.00')

        groups = collect_undocumented(self.manager, *AUG).grouped('business_unit')

        self.assertEqual([g.title for g in groups], ['גאגא', 'ללא שיוך לעסק'])
        self.assertEqual(groups[0].total, Decimal('300.00'))
        self.assertEqual(groups[1].total, Decimal('450.00'))

        by_cat = collect_undocumented(self.manager, *AUG).grouped('business_category')
        self.assertEqual(by_cat[0].title, 'גאגא · ריקוד')

    def test_merged_rows_stay_in_their_group_but_not_in_its_total(self):
        charge = make_lesson_invoice('INV-DUP', self.fam_n, self.north, 5, '320.00')
        charge.children.create(child=self.kid_n)
        FormalDocument.objects.create(
            document_number='2026-0100', document_type='receipt', client_type='existing',
            child=self.kid_n, branch=self.north, document_date=date(2026, 8, 6),
            subtotal=Decimal('320.00'), total_amount=Decimal('320.00'),
        )

        north = collect_undocumented(self.manager, *AUG).grouped('branch')[0]

        self.assertEqual(len(north.rows), 1)
        self.assertEqual(north.count, 0)
        self.assertEqual(north.total, Decimal('0.00'))
        self.assertEqual(north.merged_total, Decimal('320.00'))
