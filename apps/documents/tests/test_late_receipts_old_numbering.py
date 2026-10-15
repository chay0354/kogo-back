"""
The lesson charges of 1–11.9.2026 had only an 'INV-…' email; the owner had them
receipted late, signed (15.10.2026). These hold the three things that must stay
true when that happens: each charge gets one receipt and never a second, the
money stays in September with its receipt named beside it, and October lists
the receipts as its documents without counting their money again.
"""
import csv
import io
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.core.models import Branch, City, UserProfile
from apps.customers.checkout_invoice import CHECKOUT_LINES_ACTION
from apps.customers.financial_models import Invoice, InvoiceActivityLog
from apps.customers.models import Child, Family, Payment
from apps.documents.missing_receipts import issue_late_receipt, issue_missing_receipts, payments_without_invoice
from apps.documents.period_report import build_report
from apps.documents.tests.test_period_report import make_user
from apps.documents.undocumented_income import SOURCE_LATE_RECEIPTS, SOURCE_LESSONS, collect_undocumented

SEPT = (date(2026, 9, 1), date(2026, 9, 30))
OCT = (date(2026, 10, 1), date(2026, 10, 31))


def at(month, day, hour=9):
    return datetime(2026, month, day, hour, tzinfo=dt_timezone.utc)


class LateReceiptsForOldNumberingTests(TestCase):
    def setUp(self):
        city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='סניף צפון', city=city)
        self.family = Family.objects.create(name='משפחת צפוני', branch=self.branch)
        self.kid = self.child('נועה')
        self.sibling = self.child('דן')
        manager = make_user('manager-late@test', role=UserProfile.ROLE_MANAGER)
        self.manager = type(manager).objects.get(pk=manager.pk)

    def child(self, first):
        return Child.objects.create(family=self.family, first_name=first, last_name='צפוני',
                                    birth_date=date(2015, 5, 5), gender='female', status='active')

    def charge(self, child, amount, when, status='completed'):
        return Payment.objects.create(
            child=child, family=self.family, branch=self.branch, payment_type='recurring_subscription',
            status=status, base_amount=Decimal(amount), final_amount=Decimal(amount), payment_date=when,
        )

    def old_email(self, number, payments, when):
        """The 'INV-…' a family checkout of 1–11.9 got: one record, every charge named in its log."""
        invoice = Invoice.objects.create(
            invoice_number=number, family=self.family, branch=self.branch, payment=payments[0],
            amount=sum(p.final_amount for p in payments), status='paid', payment_method='credit_card',
            payment_type='recurring', payer_name=self.family.name, invoice_date=when,
        )
        InvoiceActivityLog.objects.create(invoice=invoice, action=CHECKOUT_LINES_ACTION,
                                          details={'lines': [], 'payment_ids': [str(p.id) for p in payments]})
        return invoice

    def test_a_charge_of_1_to_11_9_with_only_an_inv_email_is_missing_its_receipt(self):
        first = self.charge(self.kid, '350.00', at(9, 1))
        second = self.charge(self.sibling, '250.00', at(9, 1))
        self.old_email('INV-20260901-FAM-AAAA0001', [first, second], at(9, 1))
        august = self.charge(self.kid, '300.00', at(8, 20))
        self.old_email('INV-20260820-FAM-BBBB0002', [august], at(8, 20))

        missing = {str(p.id) for p in payments_without_invoice()}

        self.assertEqual(missing, {str(first.id), str(second.id)})

    def test_each_gets_one_receipt_and_never_a_second(self):
        first = self.charge(self.kid, '350.00', at(9, 1))
        second = self.charge(self.sibling, '250.00', at(9, 1))
        self.old_email('INV-20260901-FAM-AAAA0001', [first, second], at(9, 1))

        result = issue_missing_receipts([first.id, second.id], user=self.manager)
        self.assertEqual(len(result['issued']), 2, result)
        receipt = Invoice.objects.get(invoice_number=result['issued'][0]['number'])
        log = receipt.activity_logs.get(action='issued_late')
        self.assertEqual(log.details['replaces'], ['INV-20260901-FAM-AAAA0001'])
        self.assertTrue(receipt.invoice_number.startswith('IR-2026-'))

        again = issue_missing_receipts([first.id, second.id], user=self.manager)
        self.assertEqual(again['issued'], [])
        self.assertIsNone(issue_late_receipt(first, now=timezone.now()))
        self.assertEqual(payments_without_invoice(), [])

    def test_september_keeps_the_money_with_its_receipt_and_october_does_not_count_it_again(self):
        first = self.charge(self.kid, '350.00', at(9, 1))
        second = self.charge(self.sibling, '250.00', at(9, 1))
        self.old_email('INV-20260901-FAM-AAAA0001', [first, second], at(9, 1))
        sept_before = build_report(self.manager, *SEPT, 'ספטמבר')
        sept_before.undocumented = collect_undocumented(self.manager, *SEPT)
        self.assertEqual(sept_before.all_income_total, Decimal('600.00'))

        issue_missing_receipts([first.id, second.id], user=self.manager)
        # Issued "today" — in the test, move the receipts to 15.10 like the real run.
        Invoice.objects.filter(invoice_number__startswith='IR-').update(invoice_date=at(10, 15))

        sept = build_report(self.manager, *SEPT, 'ספטמבר')
        sept.undocumented = collect_undocumented(self.manager, *SEPT)
        sources = {s.source: s for s in sept.undocumented.sections}
        self.assertNotIn(SOURCE_LESSONS, sources)
        late = sources[SOURCE_LATE_RECEIPTS]
        self.assertEqual(late.total, Decimal('600.00'))
        self.assertTrue(all(row.reference.startswith('IR-2026-') for row in late.rows))
        self.assertEqual(sept.all_income_total, Decimal('600.00'))
        self.assertEqual(sept.undocumented.late_total, Decimal('600.00'))

        october = build_report(self.manager, *OCT, 'אוקטובר')
        october.undocumented = collect_undocumented(self.manager, *OCT)
        self.assertEqual(october.collected_total, Decimal('600.00'))   # the documents are October's
        self.assertEqual(october.earlier_money_total, Decimal('600.00'))
        self.assertEqual(october.all_income_total, Decimal('0.00'))   # the money was September's
        entry = october.income_by_group()[0]
        self.assertEqual(entry['documented'], Decimal('0.00'))

    def test_a_refunded_charge_in_the_same_email_stays_listed_for_its_share(self):
        first = self.charge(self.kid, '350.00', at(9, 2))
        refunded = self.charge(self.sibling, '250.00', at(9, 2), status='refunded')
        self.old_email('INV-20260902-FAM-CCCC0003', [first, refunded], at(9, 2))

        issue_missing_receipts([first.id, refunded.id], user=self.manager)
        Invoice.objects.filter(invoice_number__startswith='IR-').update(invoice_date=at(10, 15))
        sept = collect_undocumented(self.manager, *SEPT)

        lessons = next(s for s in sept.sections if s.source == SOURCE_LESSONS)
        self.assertEqual(lessons.total, Decimal('250.00'))
        late = next(s for s in sept.sections if s.source == SOURCE_LATE_RECEIPTS)
        self.assertEqual(late.total, Decimal('350.00'))

    def test_the_files_say_it_in_one_set_of_lines(self):
        from apps.documents.register import money_lines, register_csv

        first = self.charge(self.kid, '350.00', at(9, 1))
        self.old_email('INV-20260901-FAM-AAAA0001', [first], at(9, 1))
        issue_missing_receipts([first.id], user=self.manager)
        Invoice.objects.filter(invoice_number__startswith='IR-').update(invoice_date=at(10, 15))

        october = build_report(self.manager, *OCT, 'אוקטובר')
        october.undocumented = collect_undocumented(self.manager, *OCT)
        labels = dict(money_lines(october))
        self.assertEqual(labels['מזה קבלות באיחור על כסף שנכנס לפני התקופה — נספר שם'], Decimal('-350.00'))
        lines = list(csv.reader(io.StringIO(register_csv(october).decode('utf-8-sig'))))
        self.assertIn(['סה"כ כסף שנכנס בתקופה', '0.00'], lines)

        sept = build_report(self.manager, *SEPT, 'ספטמבר')
        sept.undocumented = collect_undocumented(self.manager, *SEPT)
        labels = dict(money_lines(sept))
        self.assertEqual(labels['תקבולים שהקבלה שלהם הופקה אחרי התקופה'], Decimal('350.00'))
        self.assertEqual(labels['תקבולים ללא מסמך'], Decimal('0.00'))
