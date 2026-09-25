"""
Approving a draft (F): the row is locked before it is looked at, and the
approved document is dated and stamped when it is approved.

The race is run for real, on two database connections: a TransactionTestCase,
since inside a TestCase nothing one thread writes is ever seen by another.
"""
import threading
from datetime import date, timedelta
from decimal import Decimal

from django.db import connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.customers.models import Child, Family
from apps.documents import service
from apps.documents.document_pdf import build_document_layout
from apps.documents.invoice_document import issue_stamp
from apps.documents.models import DocumentSeries, FormalDocument, SignedOriginal
from apps.documents.numbering import continuity, israel_today
from apps.documents.tests.signing_support import signing_on
from apps.documents.tests.test_drafts_and_pdf import make_user

CREATE = '/api/v1/documents/documents/create-document/'
LIST = '/api/v1/documents/documents/'


def draft_payload(child, day):
    return {
        'document_type': 'draft',
        'draft_target_type': 'tax_invoice',
        'client_type': 'existing',
        'child_id': str(child.id),
        'invoice_details': {
            'document_date': str(day),
            'line_items': [{'description': 'סדנה', 'quantity': 1, 'price': '100.00'}],
        },
    }


class DraftFixture:
    def make_child(self):
        city = City.objects.create(name='עיר')
        branch = Branch.objects.create(name='סניף', city=city)
        family = Family.objects.create(name='משפחה', branch=branch)
        return Child.objects.create(
            family=family, first_name='נועה', last_name='כהן',
            birth_date=date(2015, 5, 5), gender='female', status='active',
        )


@signing_on()
@override_settings(TRANZILA_BILLING_TERMINAL='')
class TwoApprovalsAtOnceTests(DraftFixture, TransactionTestCase):
    serialized_rollback = True

    def test_two_approvals_of_one_draft_issue_one_number_and_one_document(self):
        child = self.make_child()
        manager = make_user('race-manager@test', UserProfile.ROLE_MANAGER)
        draft = service.create_draft(draft_payload(child, israel_today()))
        year = israel_today().year

        start = threading.Barrier(2)
        results = []

        def approve():
            try:
                # Each request reads the draft before either takes the lock, as
                # the view's get_object() does: both copies say "draft".
                stale = FormalDocument.objects.get(pk=draft.pk)
                start.wait(timeout=10)
                done = service.finalize_draft(stale, issued_by=manager)
                results.append(('issued', done.document_number))
            except ValueError as exc:
                results.append(('refused', str(exc)))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=approve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(sorted(kind for kind, _ in results), ['issued', 'refused'], results)
        number = next(value for kind, value in results if kind == 'issued')
        issued = FormalDocument.objects.get(pk=draft.pk)
        self.assertEqual((issued.document_type, issued.document_number), ('tax_invoice', number))
        self.assertEqual(FormalDocument.objects.filter(document_type='tax_invoice').count(), 1)
        # The run handed out exactly one number, and that number is on the document.
        self.assertEqual(DocumentSeries.objects.get(series='TI', year=year).counter, 1)
        run = next(r for r in continuity(year) if r.series == 'TI')
        self.assertEqual((run.issued, run.missing), (1, ()))
        # One original recorded for it, under the one number it carries.
        self.assertEqual(
            list(SignedOriginal.objects.filter(source_id=str(draft.pk)).values_list('number', flat=True)),
            [number],
        )


@override_settings(TRANZILA_BILLING_TERMINAL='')
class ApprovalDateAndStampTests(DraftFixture, APITestCase):
    def setUp(self):
        self.child = self.make_child()
        self.manager = make_user('approve-manager@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)

    def draft(self, day):
        res = self.client.post(CREATE, draft_payload(self.child, day), format='json')
        self.assertEqual(res.status_code, 201, res.data)
        return FormalDocument.objects.get(pk=res.data['id'])

    def test_an_approved_draft_is_dated_and_stamped_the_day_and_moment_it_is_approved(self):
        today = israel_today()
        typed = today - timedelta(days=3) if today.day > 3 else today
        draft = self.draft(typed)
        self.assertIsNone(draft.issued_at)

        before = timezone.now()
        res = self.client.post(f'{LIST}{draft.pk}/finalize/')

        self.assertEqual(res.status_code, 200, res.data)
        doc = FormalDocument.objects.get(pk=draft.pk)
        self.assertEqual(doc.document_date, today)
        self.assertGreaterEqual(doc.issued_at, before)
        self.assertEqual(doc.issued_by_id, self.manager.pk)

    def test_a_second_approval_of_the_same_draft_is_refused(self):
        draft = self.draft(israel_today())
        first = self.client.post(f'{LIST}{draft.pk}/finalize/')
        second = self.client.post(f'{LIST}{draft.pk}/finalize/')
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(FormalDocument.objects.get(pk=draft.pk).document_number, first.data['document_number'])

    def test_a_document_issued_directly_records_when_and_by_whom(self):
        payload = draft_payload(self.child, israel_today())
        payload['document_type'] = 'tax_invoice'
        payload.pop('draft_target_type')
        res = self.client.post(CREATE, payload, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        doc = FormalDocument.objects.get(pk=res.data['id'])
        self.assertIsNotNone(doc.issued_at)
        self.assertEqual(doc.issued_by_id, self.manager.pk)

    def test_the_page_prints_the_moment_of_issue_not_the_moment_the_draft_was_typed(self):
        draft = self.draft(israel_today())
        typed = timezone.now() - timedelta(days=2, hours=3)
        FormalDocument.objects.filter(pk=draft.pk).update(created_at=typed)
        self.client.post(f'{LIST}{draft.pk}/finalize/')
        doc = FormalDocument.objects.get(pk=draft.pk)

        printed = {f.label: f.value for f in build_document_layout(doc).document_fields}['תאריך ושעה']

        self.assertEqual(printed, issue_stamp(doc.issued_at))
        self.assertNotEqual(printed, issue_stamp(typed))

    def test_a_row_from_before_issued_at_prints_when_it_was_made(self):
        draft = self.draft(israel_today())
        self.client.post(f'{LIST}{draft.pk}/finalize/')
        FormalDocument.objects.filter(pk=draft.pk).update(issued_at=None)
        doc = FormalDocument.objects.get(pk=draft.pk)
        printed = {f.label: f.value for f in build_document_layout(doc).document_fields}['תאריך ושעה']
        self.assertEqual(printed, issue_stamp(doc.created_at))

    def test_the_uniform_file_reports_the_moment_of_issue(self):
        from apps.documents.uniform_export import _manual
        from apps.documents.period_report import _row_from

        draft = self.draft(israel_today())
        FormalDocument.objects.filter(pk=draft.pk).update(
            created_at=timezone.make_aware(timezone.datetime(2026, 1, 2, 8, 15)),
        )
        self.client.post(f'{LIST}{draft.pk}/finalize/')
        stamp = timezone.make_aware(timezone.datetime(2026, 1, 5, 14, 40))
        FormalDocument.objects.filter(pk=draft.pk).update(issued_at=stamp)
        doc = (
            FormalDocument.objects.prefetch_related('line_items', 'payments', 'store_invoices')
            .select_related('business', 'business_category').get(pk=draft.pk)
        )

        uniform = _manual(_row_from(doc), doc, {})

        self.assertEqual((uniform.issue_date, uniform.issue_time.strftime('%H%M')), (date(2026, 1, 5), '1440'))

    def test_a_draft_is_not_offered_as_an_open_invoice(self):
        draft = self.draft(israel_today())
        payload = draft_payload(self.child, israel_today())
        payload['document_type'] = 'tax_invoice'
        payload.pop('draft_target_type')
        real = self.client.post(CREATE, payload, format='json')

        res = self.client.get(LIST, {'exclude_credits': 'true', 'child_id': str(self.child.id)})

        rows = res.data['results'] if isinstance(res.data, dict) else res.data
        numbers = {row['document_number'] for row in rows}
        self.assertIn(real.data['document_number'], numbers)
        self.assertNotIn(draft.document_number, numbers)
        self.assertEqual(Decimal(real.data['total_amount']), Decimal('118.00'))
