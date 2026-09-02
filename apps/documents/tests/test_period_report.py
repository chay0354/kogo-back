"""
The period report puts the whole business's revenue on one page, so it is
tested as a permission surface first and a report second: who may open it,
whether a partner's copy stops at their branches, and whether the totals it
prints are the totals the database holds.
"""
import os
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Sum
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.customers.models import Child, Family
from apps.documents.models import FormalDocument

User = get_user_model()

URL = '/api/v1/documents/documents/period-report/'


def make_user(username, role=UserProfile.ROLE_WORKER, **extra):
    user = User.objects.create_user(username=username, password='pw-for-tests', **extra)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return user


def make_document(number, child, branch, day, amount_before_vat, doc_type='tax_invoice'):
    before = Decimal(amount_before_vat)
    vat = (before * Decimal('18') / Decimal('100')).quantize(Decimal('0.01'))
    return FormalDocument.objects.create(
        document_number=number,
        document_type=doc_type,
        client_type='existing',
        child=child,
        branch=branch,
        document_date=day,
        subtotal=before,
        vat_percent=Decimal('18'),
        vat_amount=vat,
        total_amount=before + vat,
    )


class PeriodReportTests(APITestCase):
    def setUp(self):
        self.city = City.objects.create(name='עיר בדיקה')
        self.north = Branch.objects.create(name='סניף צפון', city=self.city)
        self.south = Branch.objects.create(name='סניף דרום', city=self.city)

        fam_n = Family.objects.create(name='משפחה צפון', branch=self.north)
        fam_s = Family.objects.create(name='משפחה דרום', branch=self.south)
        self.kid_n = Child.objects.create(
            family=fam_n, first_name='נועה', last_name='צפוני',
            birth_date=date(2015, 5, 5), gender='female', status='active',
        )
        self.kid_s = Child.objects.create(
            family=fam_s, first_name='דן', last_name='דרומי',
            birth_date=date(2014, 3, 3), gender='male', status='active',
        )

        # Two in the month, in different branches; one outside it; one credit.
        self.in_n = make_document('2026-0001', self.kid_n, self.north, date(2026, 8, 5), '260.00')
        self.in_s = make_document('2026-0002', self.kid_s, self.south, date(2026, 8, 20), '360.00')
        self.outside = make_document('2026-0003', self.kid_n, self.north, date(2026, 9, 2), '999.00')
        self.credit = make_document(
            '2026-0004', self.kid_s, self.south, date(2026, 8, 25), '100.00', doc_type='credit_invoice',
        )
        # A receipt settling the north invoice — the same money a second time —
        # and a transaction invoice, which is a demand for payment, not a tax
        # document. Neither may inflate revenue.
        self.receipt = make_document('2026-0005', self.kid_n, self.north, date(2026, 8, 6), '260.00', doc_type='receipt')
        self.proforma = make_document(
            '2026-0006', self.kid_n, self.north, date(2026, 8, 7), '500.00', doc_type='transaction_invoice',
        )

        self.manager = make_user('manager-report@test', role=UserProfile.ROLE_MANAGER)
        self.partner = make_user('partner-report@test', role=UserProfile.ROLE_PARTNER)
        self.partner.profile.assigned_branches.add(self.north)
        self.worker = make_user('worker-report@test', role=UserProfile.ROLE_WORKER)
        # The profile signal caches the pre-role instance on the user; the
        # permission check reads user.profile, so hand it a fresh row instead.
        self.manager = User.objects.get(pk=self.manager.pk)
        self.partner = User.objects.get(pk=self.partner.pk)
        self.worker = User.objects.get(pk=self.worker.pk)

    # --- who may open it ---------------------------------------------------

    def test_manager_gets_a_pdf(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(URL, {'month': '2026-08'})
        self.assertEqual(res.status_code, status.HTTP_200_OK, getattr(res, 'data', res.content[:200]))
        self.assertEqual(res['Content-Type'], 'application/pdf')
        self.assertTrue(res.content.startswith(b'%PDF'), res.content[:20])
        self.assertIn('attachment', res['Content-Disposition'])

    def test_partner_is_refused(self):
        self.client.force_authenticate(self.partner)
        res = self.client.get(URL, {'month': '2026-08'})
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_worker_is_refused(self):
        self.client.force_authenticate(self.worker)
        res = self.client.get(URL, {'month': '2026-08'})
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_is_refused(self):
        res = self.client.get(URL, {'month': '2026-08'})
        self.assertIn(res.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    # --- bad input is a 400, never a 500 -----------------------------------

    def test_bad_month_is_a_400(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(URL, {'month': 'august'})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', res.data)

    def test_bad_grouping_is_a_400(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(URL, {'month': '2026-08', 'group_by': 'colour'})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_period_is_a_400(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(URL)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    # --- the numbers are the database's numbers ----------------------------

    def test_totals_match_the_database_and_exclude_other_months(self):
        from apps.documents.period_report import build_report, parse_period

        start, end, label = parse_period({'month': '2026-08'})
        report = build_report(self.manager, start, end, label)

        in_month = FormalDocument.objects.filter(document_date__range=(start, end))
        self.assertEqual(in_month.count(), 5)
        # The document outside the month never reaches the report.
        all_numbers = {row.document_number for g in report.groups for row in g.rows}
        self.assertNotIn(self.outside.document_number, all_numbers)
        self.assertEqual(all_numbers, {'2026-0001', '2026-0002', '2026-0004', '2026-0005', '2026-0006'})

        # Every document lands in exactly one group.
        placed = [row.document_number for g in report.groups for row in g.rows]
        self.assertEqual(len(placed), len(set(placed)))
        self.assertEqual(len(placed), 5)

        # A group's summary is what a reader gets by adding the rows above it,
        # credits carrying their sign: south is 360 charged minus 100 credited.
        south = next(g for g in report.groups if 'דרום' in g.title)
        self.assertEqual(south.totals.net_amount, Decimal('260.00'))
        self.assertEqual(south.totals.vat_amount, Decimal('46.80'))
        self.assertEqual(south.totals.total_amount, Decimal('306.80'))
        self.assertEqual(south.totals.charges_total, Decimal('424.80'))
        self.assertEqual(south.totals.credits_total, Decimal('118.00'))
        # And the period's bottom line agrees with itself.
        self.assertEqual(report.totals.total_amount, report.totals.net_of_credits)

        # Three figures, not one. Revenue is the tax invoices less the credit:
        # 306.80 + 424.80 - 118.00. The receipt is the north invoice's money
        # arriving, so it counts as collected and not as a second sale; the
        # transaction invoice is neither.
        self.assertEqual(report.revenue_total, Decimal('613.60'))
        self.assertEqual(report.collected_total, Decimal('306.80'))
        self.assertEqual(report.non_fiscal_total, Decimal('590.00'))

        # Inside a group the rows are sectioned by type, in reading order,
        # each section summing only its own rows.
        north = next(g for g in report.groups if 'צפון' in g.title)
        self.assertEqual([sec.document_type for sec in north.sections],
                         ['tax_invoice', 'receipt', 'transaction_invoice'])
        self.assertEqual(north.sections[0].totals.total_amount, Decimal('306.80'))
        self.assertEqual(north.sections[1].totals.count, 1)
        south_types = [sec.document_type for sec in south.sections]
        self.assertEqual(south_types, ['tax_invoice', 'credit_invoice'])

    def test_empty_month_still_renders(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(URL, {'month': '2026-01'})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.content.startswith(b'%PDF'))

    def test_business_grouping_renders_with_no_business_documents(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(URL, {'month': '2026-08', 'group_by': 'business'})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.content.startswith(b'%PDF'))

    def test_custom_range_works(self):
        self.client.force_authenticate(self.manager)
        res = self.client.get(URL, {'start_date': '2026-08-01', 'end_date': '2026-09-30'})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.content.startswith(b'%PDF'))

    def test_saves_a_sample_for_inspection(self):
        """Not an assertion — leaves a real PDF where a person can open it."""
        out = os.environ.get('PERIOD_REPORT_SAMPLE')
        if not out:
            return
        self.client.force_authenticate(self.manager)
        res = self.client.get(URL, {'month': '2026-08'})
        with open(out, 'wb') as fh:
            fh.write(res.content)
