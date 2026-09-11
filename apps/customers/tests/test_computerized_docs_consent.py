"""Consent to receive tax documents by email, and the markings on the subscription invoice.

סעיף 18ב(ג) permits sending a computerized document only to a customer who
consented and has not withdrawn that consent — so the record has to survive a
withdrawal and a later re-consent without guessing. The registration widget
records it when the parent ticks the box; the office records or withdraws it
from the family card.
"""
import io
from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from pypdf import PdfReader
from rest_framework.test import APITestCase

from apps.core.computerized_docs import (
    CONSENT_SOURCE_CRM,
    CONSENT_SOURCE_WIDGET,
    check_consent,
    record_consent,
    revoke_consent,
)
from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.financial_models import Invoice
from apps.customers.models import Family
from apps.customers.subscription_invoice_pdf import generate_subscription_invoice_pdf
from apps.documents.issuer import ISSUER_NAME


class ComputerizedDocsConsentTests(TestCase):
    def setUp(self):
        self.family = TestDataFactory.create_family(email='parent@example.com')

    def test_a_family_starts_without_consent(self):
        self.assertFalse(self.family.accepts_computerized_documents)
        self.assertFalse(check_consent(self.family, 'INV-1'))

    def test_recorded_consent_counts(self):
        self.family.computerized_docs_consent_at = timezone.now()
        self.family.computerized_docs_consent_source = CONSENT_SOURCE_CRM
        self.family.save()

        self.assertTrue(self.family.accepts_computerized_documents)
        self.assertTrue(check_consent(self.family, 'INV-1'))

    def test_withdrawn_consent_stops_counting(self):
        now = timezone.now()
        self.family.computerized_docs_consent_at = now - timedelta(days=30)
        self.family.computerized_docs_consent_revoked_at = now
        self.family.save()

        self.assertFalse(self.family.accepts_computerized_documents)

    def test_consent_given_again_after_a_withdrawal_counts(self):
        now = timezone.now()
        self.family.computerized_docs_consent_revoked_at = now - timedelta(days=30)
        self.family.computerized_docs_consent_at = now
        self.family.save()

        self.assertTrue(self.family.accepts_computerized_documents)


class SubscriptionInvoicePdfMarkingsTests(TestCase):
    """תקנה 9א(א)(1)–(2) and סעיף 18ב(א) on the invoice every paying family receives."""

    def test_pdf_carries_the_issuer_line_and_both_marks(self):
        family = TestDataFactory.create_family(email='parent@example.com')
        invoice = Invoice.objects.create(
            invoice_number='INV-20260910-TEST',
            family=family,
            amount=Decimal('236.00'),
            status='paid',
            payer_name='משפחת כהן',
            invoice_date=timezone.now(),
        )

        pdf = generate_subscription_invoice_pdf(invoice)
        text = '\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(pdf)).pages)

        self.assertIn('מקור', text)
        self.assertIn('מסמך ממוחשב', text)
        self.assertIn(ISSUER_NAME, text)


class RecordConsentTests(TestCase):
    """record_consent / revoke_consent: one shape for the record, whoever takes it."""

    def setUp(self):
        self.family = TestDataFactory.create_family(email='parent@example.com')

    def test_records_the_moment_and_where_it_was_given(self):
        when = timezone.now() - timedelta(days=1)

        record_consent(self.family, CONSENT_SOURCE_WIDGET, when=when)

        self.family.refresh_from_db()
        self.assertEqual(self.family.computerized_docs_consent_at, when)
        self.assertEqual(self.family.computerized_docs_consent_source, CONSENT_SOURCE_WIDGET)
        self.assertIsNone(self.family.computerized_docs_consent_revoked_at)
        self.assertTrue(self.family.accepts_computerized_documents)

    def test_saves_the_consent_fields_only(self):
        self.family.name = 'שם שלא נשמר'

        record_consent(self.family, CONSENT_SOURCE_CRM)

        self.family.refresh_from_db()
        self.assertEqual(self.family.name, 'משפחה בדיקה')
        self.assertTrue(self.family.accepts_computerized_documents)

    def test_a_standing_consent_keeps_its_moment_and_where_it_was_given(self):
        first = timezone.now() - timedelta(days=30)
        record_consent(self.family, CONSENT_SOURCE_WIDGET, when=first)

        record_consent(self.family, CONSENT_SOURCE_CRM)

        self.family.refresh_from_db()
        self.assertEqual(self.family.computerized_docs_consent_at, first)
        self.assertEqual(self.family.computerized_docs_consent_source, CONSENT_SOURCE_WIDGET)

    def test_consent_after_a_withdrawal_starts_anew_and_clears_it(self):
        now = timezone.now()
        record_consent(self.family, CONSENT_SOURCE_WIDGET, when=now - timedelta(days=30))
        revoke_consent(self.family, when=now - timedelta(days=10))

        record_consent(self.family, CONSENT_SOURCE_CRM, when=now)

        self.family.refresh_from_db()
        self.assertEqual(self.family.computerized_docs_consent_at, now)
        self.assertEqual(self.family.computerized_docs_consent_source, CONSENT_SOURCE_CRM)
        self.assertIsNone(self.family.computerized_docs_consent_revoked_at)
        self.assertTrue(self.family.accepts_computerized_documents)

    def test_revoke_withdraws_a_standing_consent(self):
        record_consent(self.family, CONSENT_SOURCE_WIDGET, when=timezone.now() - timedelta(days=5))
        when = timezone.now()

        revoke_consent(self.family, when=when)

        self.family.refresh_from_db()
        self.assertEqual(self.family.computerized_docs_consent_revoked_at, when)
        self.assertFalse(self.family.accepts_computerized_documents)
        self.assertFalse(check_consent(self.family, 'INV-1'))

    def test_revoke_leaves_a_family_with_nothing_to_withdraw_as_it_is(self):
        revoke_consent(self.family)
        self.family.refresh_from_db()
        self.assertIsNone(self.family.computerized_docs_consent_revoked_at)

        # An earlier withdrawal keeps its date.
        now = timezone.now()
        record_consent(self.family, CONSENT_SOURCE_WIDGET, when=now - timedelta(days=30))
        revoke_consent(self.family, when=now - timedelta(days=10))
        revoke_consent(self.family, when=now)
        self.family.refresh_from_db()
        self.assertEqual(self.family.computerized_docs_consent_revoked_at, now - timedelta(days=10))

    def test_an_unknown_source_is_refused(self):
        with self.assertRaises(ValueError):
            record_consent(self.family, 'fax')
        self.family.refresh_from_db()
        self.assertFalse(self.family.accepts_computerized_documents)


def _no_discount(**kwargs):
    from apps.customers.discount_service import DiscountCalculation

    return DiscountCalculation(
        applicable_discounts=[],
        total_discount_amount=Decimal('0.00'),
        final_price=kwargs['base_price'],
        base_price=kwargs['base_price'],
    )


def _next_wednesday():
    """A date the lesson below meets on: its weekday (Wednesday), after today."""
    today = date.today()
    return today + timedelta(days=(2 - today.weekday()) % 7 or 7)


class _WidgetConsentBase(APITestCase):
    PARENT_ID = '123456782'

    def setUp(self):
        self.branch = TestDataFactory.create_branch()
        self.course = TestDataFactory.create_course(branch=self.branch, min_age=6, max_age=9)
        # day_of_week counts from Sunday: 3 is Wednesday.
        self.lesson = TestDataFactory.create_lesson(
            course=self.course, day_of_week=3, start_time=time(17, 0), end_time=time(18, 0),
        )

    def _payload(self, **overrides):
        payload = {
            'parent_id_number': self.PARENT_ID,
            'parent_first_name': 'רות',
            'parent_last_name': 'ניסן',
            'parent_phone': '0521234567',
            'parent_email': 'ruth@example.com',
            'child_first_name': 'נועה',
            'child_last_name': 'ניסן',
            'child_id_number': '111111118',
            'child_birth_date': '2018-05-05',
            'child_gender': 'female',
            'course_id': str(self.course.id),
            'lesson_id': str(self.lesson.id),
        }
        payload.update(overrides)
        return payload

    def _family(self, parent_id=PARENT_ID):
        return Family.objects.get(parent_id_number=parent_id)


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
@patch('apps.core.payment_service.TranzilaService.create_recurring_payment_request', return_value='https://pay.test/x')
@patch('apps.customers.discount_service.DiscountService.evaluate_discounts_for_payment', side_effect=_no_discount)
class WidgetRegisterConsentTests(_WidgetConsentBase):
    """The paid registration records the consent the parent ticked on the consents step."""

    def _register(self, **overrides):
        return self.client.post('/api/v1/customers/widget/register/', self._payload(**overrides), format='json')

    def test_a_ticked_box_records_consent_given_in_the_widget(self, _discount, _tranzila):
        res = self._register(computerized_docs_consent=True)

        self.assertEqual(res.status_code, 201, res.content)
        family = self._family()
        self.assertTrue(family.accepts_computerized_documents)
        self.assertEqual(family.computerized_docs_consent_source, CONSENT_SOURCE_WIDGET)

    def test_an_empty_box_or_none_at_all_records_nothing(self, _discount, _tranzila):
        # bool('false') is True — the form spelling of no must not read as yes.
        for parent_id, extra in (
            ('000000018', {'computerized_docs_consent': False}),
            ('000000026', {'computerized_docs_consent': 'false'}),
            ('000000034', {}),
        ):
            with self.subTest(extra=extra):
                res = self._register(parent_id_number=parent_id, **extra)
                self.assertEqual(res.status_code, 201, res.content)
                self.assertFalse(self._family(parent_id).accepts_computerized_documents)

    def test_an_empty_box_never_withdraws_an_earlier_consent(self, _discount, _tranzila):
        family = TestDataFactory.create_family(parent_id_number=self.PARENT_ID, branch=self.branch)
        given = timezone.now() - timedelta(days=30)
        record_consent(family, CONSENT_SOURCE_CRM, when=given)

        res = self._register(computerized_docs_consent=False)

        self.assertEqual(res.status_code, 201, res.content)
        family.refresh_from_db()
        self.assertTrue(family.accepts_computerized_documents)
        self.assertEqual(family.computerized_docs_consent_at, given)
        self.assertEqual(family.computerized_docs_consent_source, CONSENT_SOURCE_CRM)


@patch('apps.enrollments.trial_reminders.stamp_and_notify_trial_enrollment', return_value={'sent': True})
class WidgetTrialRegisterConsentTests(_WidgetConsentBase):
    """The trial registration records it the same way."""

    def _book(self, **overrides):
        return self.client.post(
            '/api/v1/customers/widget/trial-register/',
            self._payload(trial_lesson_date=_next_wednesday().isoformat(), **overrides),
            format='json',
        )

    def test_a_ticked_box_records_consent_given_in_the_widget(self, _notify):
        res = self._book(computerized_docs_consent=True)

        self.assertEqual(res.status_code, 201, res.content)
        family = self._family()
        self.assertTrue(family.accepts_computerized_documents)
        self.assertEqual(family.computerized_docs_consent_source, CONSENT_SOURCE_WIDGET)

    def test_a_free_trial_without_the_box_records_nothing(self, _notify):
        # A free trial skips the consents step, so its registration carries no box.
        res = self._book()

        self.assertEqual(res.status_code, 201, res.content)
        self.assertFalse(self._family().accepts_computerized_documents)


class FamilyComputerizedConsentActionTests(APITestCase):
    """families/{id}/computerized-consent/: the office records or withdraws the consent from the card."""

    def setUp(self):
        self.branch = TestDataFactory.create_branch(name='סניף הצפון')
        self.family = TestDataFactory.create_family(branch=self.branch, email='parent@example.com')
        manager = TestDataFactory.create_user(username='manager-consent@test')
        self.client.force_authenticate(get_user_model().objects.get(pk=manager.pk))

    def _post(self, family, body):
        return self.client.post(
            f'/api/v1/customers/families/{family.id}/computerized-consent/', body, format='json',
        )

    def test_the_office_records_consent(self):
        res = self._post(self.family, {'consent': True})

        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.data['accepts_computerized_documents'])
        self.assertEqual(res.data['computerized_docs_consent_source'], CONSENT_SOURCE_CRM)
        self.assertIsNotNone(res.data['computerized_docs_consent_at'])
        self.family.refresh_from_db()
        self.assertTrue(self.family.accepts_computerized_documents)
        self.assertEqual(self.family.computerized_docs_consent_source, CONSENT_SOURCE_CRM)

    def test_the_office_withdraws_consent(self):
        record_consent(self.family, CONSENT_SOURCE_WIDGET, when=timezone.now() - timedelta(days=3))

        res = self._post(self.family, {'consent': False})

        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(res.data['accepts_computerized_documents'])
        self.assertIsNotNone(res.data['computerized_docs_consent_revoked_at'])
        self.family.refresh_from_db()
        self.assertFalse(self.family.accepts_computerized_documents)

    def test_recording_again_keeps_the_original_timestamp(self):
        given = timezone.now() - timedelta(days=30)
        record_consent(self.family, CONSENT_SOURCE_WIDGET, when=given)

        res = self._post(self.family, {'consent': True})

        self.assertEqual(res.status_code, 200, res.content)
        self.family.refresh_from_db()
        self.assertEqual(self.family.computerized_docs_consent_at, given)
        self.assertEqual(self.family.computerized_docs_consent_source, CONSENT_SOURCE_WIDGET)

    def test_anything_but_true_or_false_changes_nothing(self):
        record_consent(self.family, CONSENT_SOURCE_WIDGET)

        for body in ({}, {'consent': None}, {'consent': 'false'}):
            with self.subTest(body=body):
                self.assertEqual(self._post(self.family, body).status_code, 400)

        self.family.refresh_from_db()
        self.assertTrue(self.family.accepts_computerized_documents)

    def test_a_partner_cannot_reach_another_branch_s_family(self):
        own_branch = TestDataFactory.create_branch(name='סניף הדרום')
        partner = TestDataFactory.create_user(username='partner-consent@test', role=UserProfile.ROLE_PARTNER)
        partner.profile.assigned_branches.add(own_branch)
        self.client.force_authenticate(get_user_model().objects.get(pk=partner.pk))

        res = self._post(self.family, {'consent': True})

        self.assertEqual(res.status_code, 404, res.content)
        self.family.refresh_from_db()
        self.assertFalse(self.family.accepts_computerized_documents)
        # A family of the partner's own branch it can reach.
        own = TestDataFactory.create_family(name='משפחה בדרום', branch=own_branch)
        self.assertEqual(self._post(own, {'consent': True}).status_code, 200)

    def test_the_card_shows_the_consent_but_an_edit_cannot_set_it(self):
        res = self.client.patch(f'/api/v1/customers/families/{self.family.id}/', {
            'notes': 'עודכן',
            'computerized_docs_consent_at': timezone.now().isoformat(),
            'computerized_docs_consent_source': CONSENT_SOURCE_CRM,
        }, format='json')

        self.assertEqual(res.status_code, 200, res.content)
        for field in (
            'computerized_docs_consent_at', 'computerized_docs_consent_source',
            'computerized_docs_consent_revoked_at', 'accepts_computerized_documents',
        ):
            self.assertIn(field, res.data)
        self.family.refresh_from_db()
        self.assertEqual(self.family.notes, 'עודכן')
        self.assertIsNone(self.family.computerized_docs_consent_at)
        self.assertFalse(self.family.accepts_computerized_documents)
