"""What the registrations say about a phone: every answer, in its order, and what is never written."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Child, Family, Parent, Payment
from apps.enrollments.models import LessonEnrollment
from apps.wahub import matching, state
from apps.wahub.models import Contact, ContactEvent
from apps.wahub.tests.base import PHONE, WahubTestCase


class MatchingTestCase(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.today = state.now_israel_date()
        self.lesson = TestDataFactory.create_lesson()
        self.incoming('אשמח לפרטים')
        self.wrote_at = self.contact().first_inbound_at

    def family(self, phone='050-5550101', **fields):
        family = TestDataFactory.create_family(name='לוי', branch=self.lesson.course.branch, phone=phone, **fields)
        TestDataFactory.create_parent(family=family, phone=phone, first_name='רותם', last_name='לוי')
        return family

    def child(self, family=None, status='pending', first_name='נועה', **fields):
        return TestDataFactory.create_child(
            family=family or self.family(), first_name=first_name, last_name='לוי', status=status, **fields,
        )

    def paid(self, child, when, *, trial=False, status='completed', description='מנוי חודשי - קפוארה', lesson='same'):
        payment = Payment.objects.create(
            child=child, family=child.family, lesson=self.lesson if lesson == 'same' else lesson,
            payment_type='one_time' if trial else 'recurring_subscription', status=status,
            base_amount=Decimal('260'), final_amount=Decimal('260'), description=description,
            trial_lesson_date=self.today if trial else None, payment_date=when if status == 'completed' else None,
        )
        Payment.objects.filter(pk=payment.pk).update(created_at=when)
        return payment

    def enrolled(self, child, *, created=None, lesson=None, **fields):
        enrollment = LessonEnrollment.objects.create(child=child, lesson=lesson or self.lesson, status='active', **fields)
        if created is not None:
            LessonEnrollment.objects.filter(pk=enrollment.pk).update(created_at=created)
        return enrollment

    def match(self):
        return matching.match_phone(PHONE, first_message_at=self.wrote_at)

    def recheck(self) -> Contact:
        matching.recheck_contact(self.contact())
        return self.contact()


class FindingTheFamilyTests(MatchingTestCase):
    def test_nobody_has_this_phone(self):
        self.child(self.family(phone='050-9999999'))
        result = self.match()
        self.assertEqual(result.outcome, 'not_found')
        self.assertIsNone(result.family_id)
        self.assertEqual(result.child_ids, [])

    def test_found_by_the_family_phone_however_it_is_stored(self):
        for stored in ('050-5550101', '0505550101', '+972 50-555-0101', '972505550101', '(050) 555 0101'):
            with self.subTest(stored=stored):
                Family.objects.all().delete()
                family = TestDataFactory.create_family(branch=self.lesson.course.branch, phone=stored)
                self.child(family)
                self.assertEqual(self.match().family_id, family.id)

    def test_found_by_any_parent_including_an_extra_contact(self):
        family = self.family(phone='050-7000000')
        Parent.objects.create(family=family, first_name='סבתא', last_name='לוי', phone='050-555-0101', is_primary=False)
        self.child(family)
        self.assertEqual(self.match().family_id, family.id)

    def test_found_by_a_childs_own_phone(self):
        family = self.family(phone='050-7000000')
        child = self.child(family, phone_number='0505550101')
        result = self.match()
        self.assertEqual(result.family_id, family.id)
        self.assertEqual(result.child_ids, [str(child.id)])

    def test_a_similar_phone_is_not_a_match(self):
        self.child(self.family(phone='050-5550102'))
        self.child(self.family(phone='0505550101999'))
        self.assertEqual(self.match().outcome, 'not_found')

    def test_a_walk_in_is_nobodys_registration(self):
        family = self.family()
        self.child(family, status='ghost')
        self.assertEqual(self.match().outcome, 'not_found')
        # A ghost with our phone on it does not bring its family in either.
        other = self.family(phone='050-7000000')
        self.child(other, status='ghost', phone_number='0505550101')
        self.assertEqual(self.match().outcome, 'not_found')

    def test_a_walk_in_beside_a_real_child_is_left_out(self):
        family = self.family()
        real = self.child(family, status='pending')
        self.child(family, status='ghost', first_name='רפאים')
        result = self.match()
        self.assertEqual(result.outcome, 'pending')
        self.assertEqual(result.child_ids, [str(real.id)])

    def test_a_family_with_no_children_is_in_the_system(self):
        family = self.family()
        result = self.match()
        self.assertEqual(result.outcome, 'in_system')
        self.assertEqual(result.family_id, family.id)


class OutcomeTests(MatchingTestCase):
    def test_registered_after_the_contact_first_wrote(self):
        child = self.child(status='active')
        self.paid(child, self.wrote_at + timedelta(days=2))
        result = self.match()
        self.assertEqual(result.outcome, 'registered_after')
        self.assertIn('נרשם ב-', result.detail)
        self.assertIn('נועה לוי', result.detail)
        self.assertEqual(result.family_id, child.family_id)

    def test_a_customer_from_before(self):
        child = self.child(status='active')
        self.paid(child, self.wrote_at - timedelta(days=40))
        self.paid(child, self.wrote_at + timedelta(days=1))     # the next month's charge changes nothing
        result = self.match()
        self.assertEqual(result.outcome, 'customer_before')
        self.assertIn('לקוח רשום מ-', result.detail)

    def test_a_child_whose_card_fails_is_still_a_customer(self):
        child = self.child(status='payment_problem')
        self.paid(child, self.wrote_at - timedelta(days=40))
        self.assertEqual(self.match().outcome, 'customer_before')

    def test_a_paid_trial_is_not_the_registration(self):
        child = self.child(status='active')
        self.paid(child, self.wrote_at - timedelta(days=30), trial=True, description='שיעור ניסיון - קפוארה')
        self.paid(child, self.wrote_at + timedelta(days=3))
        self.assertEqual(self.match().outcome, 'registered_after')

    def test_a_refunded_first_charge_still_dates_the_registration(self):
        child = self.child(status='active')
        refunded = self.paid(child, self.wrote_at - timedelta(days=30))
        Payment.objects.filter(pk=refunded.pk).update(status='refunded')
        self.paid(child, self.wrote_at + timedelta(days=3))
        self.assertEqual(self.match().outcome, 'customer_before')

    def test_with_no_charge_the_live_enrolment_dates_it(self):
        """Cash and cheques: the office registered the child by hand."""
        child = self.child(status='active')
        Child.objects.filter(pk=child.pk).update(created_at=self.wrote_at - timedelta(days=300))
        self.enrolled(child, created=self.wrote_at + timedelta(days=1))
        self.assertEqual(self.match().outcome, 'registered_after')
        LessonEnrollment.objects.update(created_at=self.wrote_at - timedelta(days=5))
        self.assertEqual(self.match().outcome, 'customer_before')

    def test_with_nothing_else_the_day_the_card_was_opened_dates_it(self):
        child = self.child(status='active')
        Child.objects.filter(pk=child.pk).update(created_at=self.wrote_at - timedelta(days=5))
        self.assertEqual(self.match().outcome, 'customer_before')
        Child.objects.filter(pk=child.pk).update(created_at=self.wrote_at + timedelta(days=5))
        self.assertEqual(self.match().outcome, 'registered_after')

    def test_one_sibling_registered_after_is_enough(self):
        family = self.family()
        old = self.child(family, status='active', first_name='איתי')
        new = self.child(family, status='active', first_name='נועה')
        self.paid(old, self.wrote_at - timedelta(days=100))
        self.paid(new, self.wrote_at + timedelta(days=2))
        result = self.match()
        self.assertEqual(result.outcome, 'registered_after')
        self.assertIn('נועה', result.detail)
        self.assertNotIn('איתי', result.detail)
        self.assertEqual(set(result.child_ids), {str(old.id), str(new.id)})

    def test_a_trial_still_ahead(self):
        child = self.child(status='trial_signed')
        self.enrolled(child, trial_lesson_date=self.today + timedelta(days=4))
        result = self.match()
        self.assertEqual(result.outcome, 'trial_upcoming')
        ahead = self.today + timedelta(days=4)
        self.assertIn(f'ניסיון ב-{ahead.day}.{ahead.month}', result.detail)

    def test_a_trial_today_is_still_ahead(self):
        child = self.child(status='trial_signed')
        self.enrolled(child, trial_lesson_date=self.today)
        self.assertEqual(self.match().outcome, 'trial_upcoming')

    def test_a_trial_that_took_place_and_how_it_went(self):
        held = self.today - timedelta(days=8)
        for outcome, words in (('attended', 'הגיע'), ('no_show', 'לא הגיע'), ('unmarked', 'לא סומן'), ('', 'לא סומן')):
            with self.subTest(outcome=outcome):
                Family.objects.all().delete()
                child = self.child(status='trial_completed')
                self.enrolled(child, trial_lesson_date=held, trial_outcome=outcome)
                result = self.match()
                self.assertEqual(result.outcome, 'trial_only')
                self.assertEqual(result.detail, f'ניסיון ב-{held.day}.{held.month}, {words} · נועה לוי')

    def test_a_trial_recorded_only_on_the_enrolment(self):
        """The child's status moved on; the register still remembers the trial."""
        child = self.child(status='inactive')
        self.enrolled(child, trial_held_on=self.today - timedelta(days=20), trial_outcome='no_show')
        result = self.match()
        self.assertEqual(result.outcome, 'trial_only')
        self.assertIn('לא הגיע', result.detail)

    def test_trial_completed_with_no_register_row(self):
        self.child(status='trial_completed')
        result = self.match()
        self.assertEqual(result.outcome, 'trial_only')
        self.assertIn('לא סומן', result.detail)

    def test_a_former_student_is_not_tried_and_did_not_register(self):
        child = self.child(status='inactive')
        self.enrolled(child, trial_held_on=self.today - timedelta(days=200), trial_outcome='attended')
        self.paid(child, self.wrote_at - timedelta(days=180))
        result = self.match()
        self.assertEqual(result.outcome, 'in_system')
        self.assertIn('לא פעיל', result.detail)

    def test_a_sign_up_whose_charge_failed(self):
        child = self.child(status='pending')
        failed = Payment.objects.create(
            child=child, family=child.family, lesson=self.lesson, payment_type='recurring_subscription',
            status='failed', base_amount=Decimal('260'), final_amount=Decimal('260'),
            failure_code='141', failure_reason='חברת האשראי סירבה לעסקה (קוד 141).',
        )
        Payment.objects.filter(pk=failed.pk).update(created_at=timezone.now() - timedelta(days=2))
        result = self.match()
        self.assertEqual(result.outcome, 'signup_declined')
        self.assertIn('החיוב נכשל', result.detail)

    def test_started_registering_and_never_paid(self):
        self.child(status='pending')
        result = self.match()
        self.assertEqual(result.outcome, 'pending')
        self.assertIn('התחיל רישום ב-', result.detail)

    def test_known_with_nothing_running(self):
        self.child(status='inactive')
        result = self.match()
        self.assertEqual(result.outcome, 'in_system')
        self.assertEqual(result.detail, 'נועה לוי (לא פעיל)')

    def test_the_order_money_first_then_a_trial_ahead_then_a_past_trial(self):
        family = self.family()
        past = self.child(family, status='trial_completed', first_name='איתי')
        self.enrolled(past, trial_lesson_date=self.today - timedelta(days=9), trial_outcome='attended')
        self.child(family, status='pending', first_name='גיל')
        self.assertEqual(self.match().outcome, 'trial_only')

        ahead = self.child(family, status='trial_signed', first_name='נועה')
        other_lesson = TestDataFactory.create_lesson(course=self.lesson.course, day_of_week=2)
        self.enrolled(ahead, lesson=other_lesson, trial_lesson_date=self.today + timedelta(days=2))
        self.assertEqual(self.match().outcome, 'trial_upcoming')

        paying = self.child(family, status='active', first_name='תמר')
        self.paid(paying, self.wrote_at - timedelta(days=30))
        self.assertEqual(self.match().outcome, 'customer_before')

    def test_two_families_on_one_phone_are_read_together(self):
        first = self.family()
        self.child(first, status='inactive', first_name='איתי')
        second = self.family()
        paying = self.child(second, status='active', first_name='נועה')
        self.paid(paying, self.wrote_at + timedelta(days=1))
        result = self.match()
        self.assertEqual(result.outcome, 'registered_after')
        self.assertEqual(result.family_id, second.id)
        self.assertEqual(len(result.child_ids), 2)


class RecheckTests(MatchingTestCase):
    def test_the_answer_is_kept_and_shown(self):
        child = self.child(status='active')
        self.paid(child, self.wrote_at + timedelta(days=2))
        contact = self.contact()
        response = self.post(f'contacts/{contact.id}/recheck/')
        self.assertEqual(response.status_code, 200)
        kogo = response.data['kogo']
        self.assertEqual(kogo['outcome'], 'registered_after')
        self.assertEqual(kogo['outcome_label'], 'נרשם אחרי הפנייה')
        self.assertEqual(kogo['family_id'], str(child.family_id))
        self.assertEqual(kogo['child_ids'], [str(child.id)])
        self.assertTrue(kogo['is_customer'])
        self.assertTrue(kogo['hidden_by_default'])
        self.assertIsNotNone(kogo['checked_at'])

        detail = self.get(f'contacts/{contact.id}/').data['kogo']
        self.assertEqual(detail['children'], [
            {'id': str(child.id), 'name': 'נועה לוי', 'status': 'active', 'status_label': 'פעיל'},
        ])

    def test_a_first_answer_is_not_a_journal_line_and_a_change_is(self):
        self.recheck()
        self.assertFalse(ContactEvent.objects.filter(kind='kogo_outcome_changed').exists())

        child = self.child(status='pending')
        self.assertEqual(self.recheck().kogo_outcome, 'pending')
        event = ContactEvent.objects.get(kind='kogo_outcome_changed')
        self.assertIn('לא נמצא במערכת', event.text)
        self.assertIn('התחיל רישום ולא סיים', event.text)
        self.assertIsNone(event.actor)

        Child.objects.filter(pk=child.pk).update(status='active')
        self.paid(child, self.wrote_at + timedelta(hours=3))
        self.assertEqual(self.recheck().kogo_outcome, 'registered_after')
        self.assertEqual(ContactEvent.objects.filter(kind='kogo_outcome_changed').count(), 2)

    def test_an_unchanged_answer_moves_only_the_time_it_was_checked(self):
        self.child(status='pending')
        first = self.recheck()
        Contact.objects.update(touched_at=timezone.now() - timedelta(minutes=5))
        touched = self.contact().touched_at
        self.assertFalse(matching.recheck_contact(self.contact()))
        second = self.contact()
        self.assertEqual(second.touched_at, touched)
        self.assertGreater(second.kogo_checked_at, first.kogo_checked_at)

    def test_matching_never_writes_a_follow_up_mark_or_a_customer_row(self):
        family = self.family()
        child = self.child(family, status='active')
        payment = self.paid(child, self.wrote_at + timedelta(days=2))
        Contact.objects.update(
            followup_status='later', followup_due=self.today + timedelta(days=3), followup_note='לחזור אחרי החגים',
            followup_by=self.manager, followup_at=timezone.now(),
        )
        marks = Contact.objects.values('followup_status', 'followup_due', 'followup_note', 'followup_by', 'followup_at').get()
        rows = {
            'family': Family.objects.values().get(pk=family.pk),
            'parent': Parent.objects.values().get(family=family),
            'child': Child.objects.values().get(pk=child.pk),
            'payment': Payment.objects.values().get(pk=payment.pk),
        }

        self.assertEqual(self.recheck().kogo_outcome, 'registered_after')

        self.assertEqual(
            Contact.objects.values('followup_status', 'followup_due', 'followup_note', 'followup_by', 'followup_at').get(),
            marks,
        )
        self.assertEqual(Family.objects.values().get(pk=family.pk), rows['family'])
        self.assertEqual(Parent.objects.values().get(family=family), rows['parent'])
        self.assertEqual(Child.objects.values().get(pk=child.pk), rows['child'])
        self.assertEqual(Payment.objects.values().get(pk=payment.pk), rows['payment'])

    def test_a_contact_that_never_wrote_is_dated_from_when_it_was_added(self):
        manual = self.make_contact(phone='972505550177', name='ידני')
        family = self.family(phone='050-5550177')
        child = self.child(family, status='active')
        self.paid(child, manual.created_at - timedelta(days=10))
        matching.recheck_contact(manual)
        self.assertEqual(Contact.objects.get(pk=manual.pk).kogo_outcome, 'customer_before')
