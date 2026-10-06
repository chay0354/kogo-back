"""
The red light on the customers list (owner, 6.10.2026): what counts as a
problem, what does not, and that the list pays a fixed price for it.

Every name here is made up.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

import apps.customers.tests.test_child_status_canonical as canonical
from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers import problem_flags
from apps.customers.child_status import resolve_child_status
from apps.customers.models import Child, Payment, RecurringPayment, TranzilaTransaction
from apps.customers.status_history_models import ChildStatusHistory
from apps.documents.models import CashPlan
from apps.enrollments.models import LessonEnrollment

User = get_user_model()
TODAY = timezone.localdate()
THIS_MONTH = TODAY.replace(day=1)


def month_before(month: date, back: int = 1) -> date:
    for _ in range(back):
        month = (month - timedelta(days=1)).replace(day=1)
    return month


def at(day: date, hour: int = 12, minute: int = 0):
    """A moment on the Israeli clock."""
    return timezone.make_aware(datetime(day.year, day.month, day.day, hour, minute))


def a_day_in(month: date, day: int = 5) -> date:
    return month.replace(day=day)


class FlagsTestCase(TestCase):
    def setUp(self):
        self.branch = TestDataFactory.create_branch()
        self.family = TestDataFactory.create_family(branch=self.branch)
        self.course = TestDataFactory.create_course(name='קפואירה צעירים', branch=self.branch)
        self.lesson = TestDataFactory.create_lesson(course=self.course, branch=self.branch)
        self._txn = 0

    # --- building blocks -------------------------------------------------------

    def child(self, first='נועם', last='בדיקה', **over):
        fields = dict(
            family=self.family, first_name=first, last_name=last,
            birth_date=date(2016, 3, 1), gender='male', status='active',
        )
        fields.update(over)
        return Child.objects.create(**fields)

    def place(self, child, lesson=None, **over):
        fields = dict(lesson=lesson or self.lesson, child=child, status='active', start_date=TODAY - timedelta(days=60))
        fields.update(over)
        return LessonEnrollment.objects.create(**fields)

    def charge(self, child, when, *, amount='260.00', lesson='same', code='000', status='completed', **over):
        self._txn += 1
        txn = TranzilaTransaction.objects.create(
            transaction_id=f'T{self._txn}', confirmation_code=f'A{self._txn}', transaction_type='recurring_charge',
            response_code=code, is_successful=True, idempotency_key=f'flags-{self._txn}',
        )
        fields = dict(
            child=child, family=child.family, lesson=self.lesson if lesson == 'same' else lesson,
            payment_type='recurring_subscription', status=status,
            base_amount=Decimal(amount), final_amount=Decimal(amount), payment_date=when,
            tranzila_transaction=txn, description='מנוי חודשי - קפואירה צעירים',
        )
        fields.update(over)
        payment = Payment.objects.create(**fields)
        Payment.objects.filter(pk=payment.pk).update(created_at=when or timezone.now())
        payment.refresh_from_db()
        return payment

    def standing_order(self, child, *, status='active', lesson='same', token='tok-1234', started=None, **over):
        started = started or month_before(THIS_MONTH, 3)
        initial = self.charge(child, at(started), lesson=lesson)
        fields = dict(
            child=child, initial_payment=initial, status=status, tranzila_token=token,
            amount=Decimal('260.00'), start_date=started,
            next_billing_date=(THIS_MONTH + timedelta(days=32)).replace(day=1),
        )
        fields.update(over)
        order = RecurringPayment.objects.create(**fields)
        RecurringPayment.objects.filter(pk=order.pk).update(created_at=at(started))
        order.refresh_from_db()
        return order

    def found(self, child, **kwargs):
        return problem_flags.problems_for_children([child], **kwargs)[child.id]

    def codes(self, child, **kwargs):
        return [problem.code for problem in self.found(child, **kwargs)]


class LoadedFactsAgreeMixin:
    """
    Every scenario the status rule is tested on, asked both ways: of the
    database question by question, and of the rows the list reads at once.
    """

    def setUp(self):
        super().setUp()

        def both_ways(child):
            by_question = resolve_child_status(child)
            records = problem_flags._load([child])
            card = records.cards.get(child.id)
            if card is not None:
                self.assertEqual(
                    resolve_child_status(child, records.facts(card)), by_question,
                    'the loaded rows and the database gave different statuses',
                )
            return by_question

        patcher = patch.object(canonical, 'resolve_child_status', both_ways)
        patcher.start()
        self.addCleanup(patcher.stop)


class LoadedFactsAgreeTest(LoadedFactsAgreeMixin, canonical.ResolveStatusTest):
    pass


class LoadedFactsAgreeSeptember27Test(LoadedFactsAgreeMixin, canonical.StatusRulesOfSeptember27Test):
    pass


class DoubleStandingOrderTest(FlagsTestCase):
    def test_two_live_orders_on_one_lesson_of_one_card(self):
        child = self.child()
        self.place(child)
        self.standing_order(child)
        self.standing_order(child)

        problem = next(p for p in self.found(child) if p.code == problem_flags.DOUBLE_STANDING_ORDER)

        self.assertIn('2 הוראות קבע פעילות', problem.what)
        self.assertIn('קפואירה צעירים', problem.what)
        self.assertIn('₪260', problem.what)
        self.assertIn('לבטל', problem.action)

    def test_one_order_on_each_card_of_the_same_child(self):
        kept = self.child()
        leftover = self.child(status='pending')
        self.place(kept)
        self.standing_order(kept)
        self.standing_order(leftover)

        problem = next(p for p in self.found(kept) if p.code == problem_flags.DOUBLE_STANDING_ORDER)

        self.assertIn(problem_flags.OTHER_CARD, problem.what)

    def test_a_brother_with_his_own_order_is_not_a_double(self):
        child = self.child()
        brother = self.child(first='איתי')
        for each in (child, brother):
            self.place(each)
            self.standing_order(each)

        self.assertNotIn(problem_flags.DOUBLE_STANDING_ORDER, self.codes(child))

    def test_a_cancelled_order_beside_a_live_one_is_not_a_double(self):
        child = self.child()
        self.place(child)
        self.standing_order(child)
        self.standing_order(child, status='cancelled')

        self.assertNotIn(problem_flags.DOUBLE_STANDING_ORDER, self.codes(child))

    def test_two_lessons_two_orders_is_how_twice_a_week_looks(self):
        child = self.child()
        other_day = TestDataFactory.create_lesson(course=self.course, branch=self.branch, day_of_week=3)
        self.place(child)
        self.place(child, lesson=other_day)
        self.standing_order(child)
        self.standing_order(child, lesson=other_day)

        self.assertNotIn(problem_flags.DOUBLE_STANDING_ORDER, self.codes(child))


class DoubleChargeTest(FlagsTestCase):
    def setUp(self):
        super().setUp()
        self.last_month = month_before(THIS_MONTH)

    def test_the_two_cards_of_one_child_charged_in_the_same_month(self):
        kept = self.child()
        leftover = self.child(status='pending')
        self.place(kept)
        self.charge(kept, at(a_day_in(self.last_month, 1)))
        self.charge(leftover, at(a_day_in(self.last_month, 1)))

        problem = next(p for p in self.found(kept) if p.code == problem_flags.DOUBLE_CHARGE)

        self.assertIn('2 חיובים חודשיים', problem.what)
        self.assertIn(problem_flags._month_label(self.last_month), problem.what)
        self.assertIn(problem_flags.OTHER_CARD, problem.what)
        self.assertIn('זיכוי', problem.action)

    def test_the_first_month_of_two_cards_is_caught_too(self):
        """Both signed in one month, both first charged the next: nothing before it excuses it."""
        kept = self.child()
        leftover = self.child(status='pending')
        self.place(kept)
        started = month_before(self.last_month)
        for each in (kept, leftover):
            self.standing_order(each, started=a_day_in(started, 20))
            self.charge(each, at(a_day_in(self.last_month, 1)))

        self.assertIn(problem_flags.DOUBLE_CHARGE, self.codes(kept))

    def test_one_card_charged_twice_in_a_month(self):
        child = self.child()
        self.place(child)
        self.charge(child, at(a_day_in(month_before(self.last_month), 1)))
        self.charge(child, at(a_day_in(self.last_month, 1)))
        self.charge(child, at(a_day_in(self.last_month, 2)))

        self.assertIn(problem_flags.DOUBLE_CHARGE, self.codes(child))

    def test_a_month_caught_up_is_not_a_double_charge(self):
        """Last month was never collected; it and this one both come off in this one."""
        child = self.child()
        self.place(child)
        self.charge(child, at(a_day_in(month_before(self.last_month, 2), 1)))
        # nothing two months ago's successor: the month before last went unpaid
        self.charge(child, at(a_day_in(self.last_month, 3)))
        self.charge(child, at(a_day_in(self.last_month, 3)))

        self.assertNotIn(problem_flags.DOUBLE_CHARGE, self.codes(child))

    def test_a_card_replacement_names_the_month_it_pays_for(self):
        child = self.child()
        self.place(child)
        earlier = month_before(self.last_month)
        self.charge(child, at(a_day_in(month_before(earlier), 1)))
        self.charge(child, at(a_day_in(self.last_month, 9)), description=f'מנוי חודשי {earlier:%m/%Y} - קפואירה צעירים')
        self.charge(child, at(a_day_in(self.last_month, 9)),
                    description=f'מנוי חודשי {self.last_month:%m/%Y} - קפואירה צעירים')

        self.assertNotIn(problem_flags.DOUBLE_CHARGE, self.codes(child))

    def test_a_refunded_one_is_no_longer_double(self):
        child = self.child()
        self.place(child)
        self.charge(child, at(a_day_in(month_before(self.last_month), 1)))
        self.charge(child, at(a_day_in(self.last_month, 1)))
        self.charge(child, at(a_day_in(self.last_month, 2)), status='refunded')

        self.assertNotIn(problem_flags.DOUBLE_CHARGE, self.codes(child))

    def test_a_charge_the_card_company_refused_took_no_money_and_is_not_a_second_charge(self):
        child = self.child()
        self.place(child)
        self.charge(child, at(a_day_in(month_before(self.last_month), 1)))
        self.charge(child, at(a_day_in(self.last_month, 1)), code='141')
        self.charge(child, at(a_day_in(self.last_month, 2)))

        codes = self.codes(child)

        self.assertNotIn(problem_flags.DOUBLE_CHARGE, codes)
        self.assertIn(problem_flags.DECLINED_RECORDED_PAID, codes)

    def test_the_registration_fee_is_not_a_month(self):
        child = self.child()
        self.place(child)
        self.charge(child, at(a_day_in(self.last_month, 1)), amount='150.00', registration_fee=Decimal('150.00'))
        self.charge(child, at(a_day_in(self.last_month, 2)))

        self.assertNotIn(problem_flags.DOUBLE_CHARGE, self.codes(child))

    def test_half_past_midnight_on_the_first_is_the_new_month(self):
        kept = self.child()
        leftover = self.child(status='pending')
        self.place(kept)
        self.charge(kept, at(self.last_month, 0, 30))
        self.charge(leftover, at(a_day_in(self.last_month, 20)))

        problem = next(p for p in self.found(kept) if p.code == problem_flags.DOUBLE_CHARGE)

        self.assertIn(problem_flags._month_label(self.last_month), problem.what)

    def test_two_lessons_charged_in_one_month_is_two_classes(self):
        child = self.child()
        other_day = TestDataFactory.create_lesson(course=self.course, branch=self.branch, day_of_week=3)
        self.place(child)
        self.charge(child, at(a_day_in(self.last_month, 1)))
        self.charge(child, at(a_day_in(self.last_month, 1)), lesson=other_day)

        self.assertNotIn(problem_flags.DOUBLE_CHARGE, self.codes(child))


class TextTheCardDrawsTest(FlagsTestCase):
    """
    The card shows the title alone and opens the two texts under it (owner,
    6.10.2026). They carry two marks: **bold**, and "• " at the head of a list
    line. The title carries neither.
    """

    def double_charge(self):
        kept = self.child(paid_until_date=TODAY + timedelta(days=12))
        leftover = self.child(status='pending')
        self.place(kept)
        last_month = month_before(THIS_MONTH)
        self.charge(kept, at(a_day_in(last_month, 1)))
        self.charge(leftover, at(a_day_in(last_month, 1)))
        return next(p for p in self.found(kept) if p.code == problem_flags.DOUBLE_CHARGE)

    def test_each_charge_is_a_line_of_its_own_with_the_sum_in_bold(self):
        problem = self.double_charge()

        first, *charges = problem.what.split('\n')
        self.assertIn('**2 חיובים חודשיים**', first)
        self.assertEqual(len(charges), 2)
        for line in charges:
            self.assertTrue(line.startswith('• '), line)
            self.assertIn('**₪260**', line)

    def test_each_thing_to_do_is_a_line_of_its_own(self):
        problem = self.double_charge()

        steps = problem.action.split('\n')
        self.assertEqual(len(steps), 2)
        self.assertTrue(steps[0].startswith('• **לזכות את החיוב המיותר**'), steps[0])

    def test_the_marks_are_always_closed_and_the_title_has_none(self):
        child = self.child(status='payment_problem')
        self.place(child)
        self.standing_order(child, status='failed', next_billing_date=month_before(THIS_MONTH))
        problems = self.found(child) + [self.double_charge()]

        self.assertTrue(problems)
        for problem in problems:
            self.assertNotIn('**', problem.title)
            self.assertNotIn('•', problem.title)
            for text in (problem.what, problem.action):
                for line in text.split('\n'):
                    self.assertEqual(line.count('**') % 2, 0, line)
                    self.assertTrue(line.strip(), text)


class UnpaidTest(FlagsTestCase):
    def test_a_failed_order_says_how_much_is_open(self):
        child = self.child(status='payment_problem')
        self.place(child)
        self.standing_order(child, status='failed', next_billing_date=month_before(THIS_MONTH))

        problem = next(p for p in self.found(child) if p.code == problem_flags.STANDING_ORDER_FAILED)

        self.assertIn(problem_flags._month_label(month_before(THIS_MONTH)), problem.what)
        self.assertIn(problem_flags._month_label(THIS_MONTH), problem.what)
        self.assertIn('₪520', problem.what)
        self.assertIn('פתוח לתשלום', problem.what)
        self.assertIn('החלפת כרטיס אשראי', problem.action)

    def test_a_child_moved_to_another_class_keeps_the_order_of_the_first(self):
        """The order still names the class they signed up to; it is the one paying for the new one."""
        other_course = TestDataFactory.create_course(name='ריקוד', branch=self.branch)
        other_lesson = TestDataFactory.create_lesson(course=other_course, branch=self.branch)
        child = self.child(status='payment_problem')
        self.place(child, lesson=other_lesson)
        self.standing_order(child, status='failed', next_billing_date=THIS_MONTH)

        self.assertIn(problem_flags.STANDING_ORDER_FAILED, self.codes(child))

    def test_an_order_left_from_a_class_the_child_left_is_not_chased(self):
        other_course = TestDataFactory.create_course(name='ריקוד', branch=self.branch)
        other_lesson = TestDataFactory.create_lesson(course=other_course, branch=self.branch)
        child = self.child(paid_until_date=TODAY + timedelta(days=10))
        self.place(child)
        self.standing_order(child)
        self.standing_order(child, status='failed', lesson=other_lesson, next_billing_date=THIS_MONTH)

        self.assertEqual(self.codes(child), [])

    def test_a_month_the_other_card_paid_is_not_open(self):
        kept = self.child(status='payment_problem')
        leftover = self.child(status='pending')
        self.place(kept)
        self.standing_order(kept, status='failed', next_billing_date=THIS_MONTH)
        self.charge(leftover, at(THIS_MONTH, 9))

        problem = next(p for p in self.found(kept) if p.code == problem_flags.STANDING_ORDER_FAILED)

        self.assertNotIn('פתוח לתשלום', problem.what)

    def test_an_order_days_late_is_a_problem(self):
        child = self.child()
        self.place(child)
        self.standing_order(child, next_billing_date=TODAY - timedelta(days=10))

        problem = next(p for p in self.found(child) if p.code == problem_flags.STANDING_ORDER_OVERDUE)

        self.assertIn('לא חויבה', problem.what)

    def test_a_charge_due_yesterday_is_still_on_its_way(self):
        child = self.child()
        self.place(child)
        self.standing_order(child, next_billing_date=TODAY - timedelta(days=1))

        self.assertEqual(self.codes(child), [])

    def test_a_charge_the_gateway_never_answered_holds_the_order(self):
        child = self.child()
        self.place(child)
        order = self.standing_order(child, next_billing_date=TODAY)
        TranzilaTransaction.objects.create(
            transaction_id='', transaction_type='recurring_charge', is_successful=False,
            idempotency_key=f'recurring_{order.id}_{TODAY.isoformat()}',
        )

        problem = next(p for p in self.found(child) if p.code == problem_flags.STANDING_ORDER_OVERDUE)

        self.assertIn('לא קיבל תשובה', problem.what)

    def test_a_live_order_with_no_card_will_never_charge(self):
        child = self.child()
        self.place(child)
        self.standing_order(child, token='')

        self.assertIn(problem_flags.STANDING_ORDER_NO_CARD, self.codes(child))

    def test_a_student_with_nothing_set_up_and_no_payment_this_month(self):
        child = self.child()
        self.place(child)
        self.charge(child, at(a_day_in(month_before(THIS_MONTH), 3)))

        problem = next(p for p in self.found(child) if p.code == problem_flags.NO_STANDING_ORDER)

        self.assertIn('קפואירה צעירים', problem.what)
        self.assertIn(problem_flags._month_label(THIS_MONTH), problem.what)
        self.assertIn('התשלום האחרון', problem.what)

    def test_a_student_who_never_paid_anything(self):
        child = self.child()
        self.place(child)

        problem = next(p for p in self.found(child) if p.code == problem_flags.NO_STANDING_ORDER)

        self.assertIn('לא נגבה אף תשלום', problem.what)

    def test_cash_is_a_way_of_paying(self):
        child = self.child()
        self.place(child)
        CashPlan.objects.create(
            child=child, lesson=self.lesson, status='active',
            total_amount=Decimal('2600.00'), monthly_amount=Decimal('260.00'),
        )

        self.assertEqual(self.codes(child), [])

    def test_a_month_paid_up_is_not_chased(self):
        child = self.child(paid_until_date=TODAY + timedelta(days=10))
        self.place(child)

        self.assertNotIn(problem_flags.NO_STANDING_ORDER, self.codes(child))

    def test_a_sign_up_that_never_finished_is_not_a_card_problem(self):
        child = self.child(status='pending')
        self.place(child)

        self.assertEqual(self.codes(child), [])

    def test_a_trial_is_not_a_place_to_pay_for(self):
        child = self.child(status='trial_signed')
        self.place(child, trial_lesson_date=TODAY + timedelta(days=3))

        self.assertEqual(self.codes(child), [])

    def test_a_healthy_order_is_no_problem(self):
        child = self.child(paid_until_date=TODAY + timedelta(days=10))
        self.place(child)
        self.standing_order(child)

        self.assertEqual(self.codes(child), [])


class DeclinedRecordedPaidTest(FlagsTestCase):
    def test_a_completed_payment_the_card_company_refused(self):
        child = self.child(paid_until_date=TODAY + timedelta(days=10))
        self.place(child)
        self.standing_order(child)
        self.charge(child, at(TODAY - timedelta(days=40)), amount='120.00', code='141')

        problem = next(p for p in self.found(child) if p.code == problem_flags.DECLINED_RECORDED_PAID)

        self.assertIn('₪120', problem.what)
        self.assertIn('141', problem.what)
        self.assertIn('לא לזכות', problem.action)

    def test_an_approval_an_old_row_and_a_local_failure_are_not_refusals(self):
        child = self.child(paid_until_date=TODAY + timedelta(days=10))
        self.place(child)
        self.standing_order(child)
        for code in ('000', '0', '', '999', 'N/A'):
            self.charge(child, at(TODAY - timedelta(days=400)), code=code)

        self.assertNotIn(problem_flags.DECLINED_RECORDED_PAID, self.codes(child))


class StuckChargeTest(FlagsTestCase):
    def test_a_charge_in_processing_for_days(self):
        child = self.child(paid_until_date=TODAY + timedelta(days=10))
        self.place(child)
        self.standing_order(child)
        payment = self.charge(child, None, status='processing')
        Payment.objects.filter(pk=payment.pk).update(updated_at=timezone.now() - timedelta(days=3))

        problem = next(p for p in self.found(child) if p.code == problem_flags.STUCK_CHARGE)

        self.assertIn('בבדיקה', problem.what)
        self.assertIn('לבדוק בטרנזילה', problem.action)

    def test_a_monthly_charge_left_pending_for_days(self):
        child = self.child(paid_until_date=TODAY + timedelta(days=10))
        self.place(child)
        self.standing_order(child)
        payment = self.charge(child, None, status='pending')
        Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(days=3))

        self.assertIn(problem_flags.STUCK_CHARGE, self.codes(child))

    def test_a_charge_made_an_hour_ago_is_still_on_its_way(self):
        child = self.child(paid_until_date=TODAY + timedelta(days=10))
        self.place(child)
        self.standing_order(child)
        payment = self.charge(child, None, status='pending')
        Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(hours=1))

        self.assertNotIn(problem_flags.STUCK_CHARGE, self.codes(child))

    def test_a_sign_up_nobody_paid_for_stays_pending_and_is_not_a_problem(self):
        child = self.child(status='pending')
        self.place(child)
        payment = self.charge(child, None, status='pending')
        Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(days=30))

        self.assertEqual(self.codes(child), [])


class StatusMismatchTest(FlagsTestCase):
    def test_a_child_who_pays_and_is_not_active(self):
        child = self.child(status='pending', paid_until_date=TODAY + timedelta(days=12))

        problem = next(p for p in self.found(child) if p.code == problem_flags.STATUS_MISMATCH)

        self.assertIn('בתהליך רישום', problem.what)
        self.assertIn('פעיל', problem.what)
        self.assertIn('שולם עליו עד', problem.what)
        self.assertIn('לשנות את הסטטוס', problem.action)

    def test_a_status_set_by_hand_says_so(self):
        child = self.child(status='inactive', paid_until_date=TODAY + timedelta(days=12))
        ChildStatusHistory.objects.create(
            child=child, previous_status='active', new_status='inactive', reason='שינוי ידני: עזב את החוג',
        )

        problem = next(p for p in self.found(child) if p.code == problem_flags.STATUS_MISMATCH)

        self.assertIn('נקבע ביד במשרד', problem.what)
        self.assertIn('עזב את החוג', problem.what)

    def test_a_charge_not_made_yet_is_not_a_card_problem(self):
        """The first days of a month: the paid period ended, the standing order has not run."""
        child = self.child(paid_until_date=TODAY - timedelta(days=2))
        self.place(child)
        self.standing_order(child, next_billing_date=TODAY)

        self.assertEqual(self.codes(child), [])

    def test_a_status_that_agrees_with_the_records(self):
        child = self.child(paid_until_date=TODAY + timedelta(days=12))

        self.assertNotIn(problem_flags.STATUS_MISMATCH, self.codes(child))

    def test_a_walk_in_gets_no_flags(self):
        ghost = self.child(status='ghost')

        self.assertEqual(self.codes(ghost), [])


class DuplicateCardTest(FlagsTestCase):
    def test_another_card_of_the_child_that_carries_money(self):
        kept = self.child(paid_until_date=TODAY + timedelta(days=12))
        leftover = self.child(status='pending', first_name='נועם')
        self.place(kept)
        self.standing_order(kept)
        self.charge(leftover, at(TODAY - timedelta(days=70)), amount='150.00', payment_type='one_time', lesson=None)

        problem = next(p for p in self.found(kept) if p.code == problem_flags.DUPLICATE_CARD)

        self.assertIn('כרטיס נוסף', problem.what)
        self.assertIn('חיוב אחד', problem.what)
        self.assertIn('₪150', problem.what)
        self.assertIn(problem_flags.OTHER_CARD_LABEL, problem.action)

    def test_a_leftover_card_with_nothing_on_it_is_not_a_problem(self):
        kept = self.child(paid_until_date=TODAY + timedelta(days=12))
        self.child(status='pending')
        self.place(kept)
        self.standing_order(kept)

        self.assertEqual(self.codes(kept), [])

    def test_the_name_is_matched_whatever_the_case(self):
        kept = self.child(first='Noam', last='Test', paid_until_date=TODAY + timedelta(days=12))
        leftover = self.child(first='noam', last='TEST', status='pending')
        self.charge(leftover, at(TODAY - timedelta(days=70)))

        self.assertIn(problem_flags.DUPLICATE_CARD, self.codes(kept))

    def test_a_sister_is_not_another_card(self):
        child = self.child(paid_until_date=TODAY + timedelta(days=12))
        sister = self.child(first='מאיה', gender='female')
        self.charge(sister, at(TODAY - timedelta(days=70)))

        self.assertEqual(self.codes(child), [])

    def test_the_card_endpoint_names_the_other_card(self):
        kept = self.child(paid_until_date=TODAY + timedelta(days=12))
        leftover = self.child(status='pending')
        self.charge(leftover, at(TODAY - timedelta(days=70)))

        detail = problem_flags.child_problem_detail(kept)

        self.assertEqual([card['id'] for card in detail['duplicate_cards']], [str(leftover.id)])
        self.assertEqual(detail['duplicate_cards'][0]['payments_count'], 1)
        self.assertEqual(detail['duplicate_cards'][0]['status_label'], 'בתהליך רישום')


class PartnerSeesOwnBranchesTest(FlagsTestCase):
    def test_a_problem_about_another_branchs_lesson_is_not_shown_to_a_partner(self):
        elsewhere = TestDataFactory.create_branch(name='סניף אחר')
        other_course = TestDataFactory.create_course(name='ריקוד', branch=elsewhere)
        other_lesson = TestDataFactory.create_lesson(course=other_course, branch=elsewhere)
        child = self.child(status='payment_problem')
        self.place(child, lesson=other_lesson)
        self.standing_order(child, status='failed', lesson=other_lesson, next_billing_date=THIS_MONTH)

        self.assertIn(problem_flags.STANDING_ORDER_FAILED, self.codes(child))
        self.assertIn(problem_flags.STANDING_ORDER_FAILED, self.codes(child, branch_ids=[elsewhere.id]))
        self.assertNotIn(problem_flags.STANDING_ORDER_FAILED, self.codes(child, branch_ids=[self.branch.id]))


class ListAndCardApiTest(FlagsTestCase):
    def setUp(self):
        super().setUp()
        manager = TestDataFactory.create_user(username='flags-manager@kogo.test')
        self.client = APIClient()
        # Read again: the profile cached on the new user predates its role.
        self.client.force_authenticate(User.objects.get(pk=manager.pk))

    def rows(self, **params):
        response = self.client.get('/api/v1/customers/children/', params)
        self.assertEqual(response.status_code, 200)
        return response.data['results']

    def test_each_row_carries_the_count_and_the_titles(self):
        troubled = self.child(first='רוני', status='pending', paid_until_date=TODAY + timedelta(days=12))
        fine = self.child(first='גיל', paid_until_date=TODAY + timedelta(days=12))

        by_id = {row['id']: row for row in self.rows()}

        self.assertEqual(by_id[str(troubled.id)]['problems_count'], 1)
        self.assertEqual(by_id[str(troubled.id)]['problem_titles'], ['הסטטוס לא תואם את הרישומים'])
        self.assertEqual(by_id[str(fine.id)]['problems_count'], 0)
        self.assertEqual(by_id[str(fine.id)]['problem_titles'], [])

    def test_only_with_problems(self):
        troubled = self.child(first='רוני', status='pending', paid_until_date=TODAY + timedelta(days=12))
        self.child(first='גיל', paid_until_date=TODAY + timedelta(days=12))

        rows = self.rows(has_problems='1')

        self.assertEqual([row['id'] for row in rows], [str(troubled.id)])

    def test_the_filter_counts_and_pages_like_the_list(self):
        for index in range(23):
            self.child(first=f'ילד{index}', status='pending', paid_until_date=TODAY + timedelta(days=12))
        self.child(first='גיל', paid_until_date=TODAY + timedelta(days=12))

        response = self.client.get('/api/v1/customers/children/', {'has_problems': '1'})
        ids = self.client.get('/api/v1/customers/children/ids/', {'has_problems': '1'})

        self.assertEqual(response.data['count'], 23)
        self.assertEqual(len(response.data['results']), 20)
        self.assertEqual(ids.data['count'], 23)

    def test_the_filter_follows_the_other_filters(self):
        self.child(first='רוני', status='pending', paid_until_date=TODAY + timedelta(days=12))

        self.assertEqual(self.rows(has_problems='1', status='active'), [])

    def test_the_child_shown_carries_the_other_cards_problem(self):
        kept = self.child(paid_until_date=TODAY + timedelta(days=12))
        leftover = self.child(status='pending')
        self.place(kept)
        last_month = month_before(THIS_MONTH)
        self.charge(kept, at(a_day_in(last_month, 1)))
        self.charge(leftover, at(a_day_in(last_month, 1)))

        rows = self.rows()

        self.assertEqual([row['id'] for row in rows], [str(kept.id)])
        self.assertIn('חיוב כפול באותו חודש', rows[0]['problem_titles'])

    def test_the_cards_endpoint_gives_what_happened_and_what_to_do(self):
        child = self.child(status='pending', paid_until_date=TODAY + timedelta(days=12))

        response = self.client.get(f'/api/v1/customers/children/{child.id}/problems/')

        self.assertEqual(response.status_code, 200)
        (problem,) = response.data['problems']
        self.assertEqual(set(problem), {'code', 'title', 'what', 'action'})
        self.assertEqual(problem['code'], problem_flags.STATUS_MISMATCH)
        self.assertEqual(response.data['duplicate_cards'], [])

    def test_the_other_cards_standing_orders_come_with_the_answer_and_nothing_is_written(self):
        """
        The card lists the other card's standing orders without asking
        customers/recurring-payments/ — whose every read writes amounts due.
        """
        kept = self.child(paid_until_date=TODAY + timedelta(days=12))
        leftover = self.child(status='pending')
        self.place(kept)
        self.standing_order(kept)
        theirs = self.standing_order(leftover)
        # An amount due to take effect: the writing route would promote it.
        RecurringPayment.objects.filter(pk=theirs.pk).update(
            pending_amount=Decimal('300.00'), pending_amount_effective_date=TODAY - timedelta(days=1),
        )

        response = self.client.get(f'/api/v1/customers/children/{kept.id}/problems/')

        (card,) = response.data['duplicate_cards']
        (order,) = card['standing_orders']
        self.assertEqual(order['id'], str(theirs.id))
        self.assertEqual(order['status'], 'active')
        self.assertEqual(order['amount'], '260.00')
        theirs.refresh_from_db()
        self.assertEqual(theirs.amount, Decimal('260.00'))
        self.assertEqual(theirs.pending_amount, Decimal('300.00'))

    def test_the_list_costs_the_same_for_two_children_and_for_twelve(self):
        def populate(count, prefix):
            for index in range(count):
                child = self.child(first=f'{prefix}{index}', status='payment_problem')
                self.place(child)
                self.standing_order(child, status='failed', next_billing_date=month_before(THIS_MONTH))

        def queries_of_the_flags():
            children = list(Child.objects.all())
            with CaptureQueriesContext(connection) as captured:
                found = problem_flags.problems_for_children(children)
            self.assertTrue(all(found.values()))
            return len(captured)

        populate(2, 'א')
        for_two = queries_of_the_flags()
        populate(10, 'ב')
        for_twelve = queries_of_the_flags()

        self.assertEqual(for_two, for_twelve)
        self.assertLessEqual(for_twelve, 16)

    def test_everyone_at_once_gives_the_same_answers_as_a_page(self):
        """The filter asks about every child: past a size the tables are read whole, not by ids."""
        kept = self.child(paid_until_date=TODAY + timedelta(days=12))
        leftover = self.child(status='pending')
        self.place(kept)
        last_month = month_before(THIS_MONTH)
        for each in (kept, leftover):
            self.standing_order(each)
            self.charge(each, at(a_day_in(last_month, 1)))
        other_family = TestDataFactory.create_family(name='משפחה אחרת', branch=self.branch)
        failed = self.child(family=other_family, first='שחר', status='payment_problem')
        self.place(failed)
        self.standing_order(failed, status='failed', next_billing_date=last_month)
        fine = self.child(family=other_family, first='גיל', paid_until_date=TODAY + timedelta(days=12))
        children = [kept, failed, fine]

        def answers():
            found = problem_flags.problems_for_children(children)
            return {child.id: [(p.code, p.what) for p in found[child.id]] for child in children}

        by_ids = answers()
        with patch.object(problem_flags, 'READ_WHOLE_ABOVE', 0):
            read_whole = answers()

        self.assertEqual(read_whole, by_ids)
        self.assertTrue(by_ids[kept.id] and by_ids[failed.id])
        self.assertEqual(by_ids[fine.id], [])

    def test_a_break_in_the_flags_does_not_take_the_list_down(self):
        child = self.child()
        with patch('apps.customers.problem_flags.problems_for_children', side_effect=RuntimeError('boom')):
            rows = self.rows()

        self.assertEqual(rows[0]['id'], str(child.id))
        self.assertIsNone(rows[0]['problems_count'])
        self.assertIsNone(rows[0]['problem_titles'])

    def test_nothing_is_written(self):
        child = self.child(status='pending', paid_until_date=TODAY + timedelta(days=12))
        self.place(child)
        self.standing_order(child, status='failed', next_billing_date=month_before(THIS_MONTH))
        before = (child.status, child.updated_at, RecurringPayment.objects.get().updated_at)

        self.rows(has_problems='1')
        self.client.get(f'/api/v1/customers/children/{child.id}/problems/')

        child.refresh_from_db()
        self.assertEqual((child.status, child.updated_at, RecurringPayment.objects.get().updated_at), before)
        self.assertFalse(ChildStatusHistory.objects.exists())


class WhoMaySeeTest(FlagsTestCase):
    def as_role(self, role, branches=()):
        user = TestDataFactory.create_user(username=f'flags-{role}@kogo.test', role=role)
        user.profile.assigned_branches.set(branches)
        client = APIClient()
        client.force_authenticate(User.objects.get(pk=user.pk))
        return client

    def test_a_worker_is_refused(self):
        child = self.child()
        client = self.as_role(UserProfile.ROLE_WORKER)

        self.assertEqual(client.get(f'/api/v1/customers/children/{child.id}/problems/').status_code, 403)
        self.assertEqual(client.get('/api/v1/customers/children/', {'has_problems': '1'}).status_code, 403)

    def test_a_partner_reaches_a_child_of_their_branch_only(self):
        mine = self.child(status='pending', paid_until_date=TODAY + timedelta(days=12))
        elsewhere = TestDataFactory.create_branch(name='סניף אחר')
        other_family = TestDataFactory.create_family(name='משפחה אחרת', branch=elsewhere)
        theirs = self.child(family=other_family, first='שחר', status='pending',
                            paid_until_date=TODAY + timedelta(days=12))
        client = self.as_role(UserProfile.ROLE_PARTNER, [self.branch])

        self.assertEqual(client.get(f'/api/v1/customers/children/{mine.id}/problems/').status_code, 200)
        self.assertEqual(client.get(f'/api/v1/customers/children/{theirs.id}/problems/').status_code, 404)
        rows = client.get('/api/v1/customers/children/', {'has_problems': '1'}).data['results']
        self.assertEqual([row['id'] for row in rows], [str(mine.id)])
