"""
Folding duplicate children into the one that stays.

Each case here is one that production held on the day this was written: a
walk-in whose parent registered afterwards, a surname the instructor typed
wrong, two siblings on one family phone, and a girl with five records from
repeat trial bookings. The money tests matter most. A merge deletes rows, and a
row that a payment points at must never be one of them.
"""
from datetime import date, time, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.core.models import Branch, City, Room
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.child_merge import resolve_duplicates
from apps.customers.models import Child, Family, Parent, Payment, RecurringPayment
from apps.enrollments.ghost_students import create_ghost_enrollment
from apps.enrollments.models import LessonAttendance, LessonEnrollment
from apps.instructors.models import Instructor


class MergeTestBase(TestCase):
    def setUp(self):
        city = City.objects.create(name='עיר')
        self.branch = Branch.objects.create(name='קניון דמרי סנטר', city=city)
        self.other_branch = Branch.objects.create(name='סניף אחר', city=city)
        room = Room.objects.create(name='סטודיו', branch=self.branch, capacity=20)
        ctype = CourseType.objects.create(name='סלינג')
        instructor = Instructor.objects.create(
            first_name='מעיין', last_name='ברץ', email='m@merge.test', primary_branch=self.branch,
        )
        self.lesson = self._lesson(ctype, instructor, room, 'א-ב סלינג', day=4)
        self.other_lesson = self._lesson(ctype, instructor, room, 'א-ב ריקוד', day=1)
        self.past_thursday = date.today() - timedelta(days=(date.today().weekday() - 3) % 7 or 7)

    def _lesson(self, ctype, instructor, room, name, *, day):
        course = Course.objects.create(
            name=name, branch=self.branch, course_type=ctype, price=Decimal('235.00'),
            capacity=20, instructor=instructor, is_active=True,
        )
        return Lesson.objects.create(
            course=course, instructor=instructor, room=room, day_of_week=day,
            start_time=time(16, 30), end_time=time(17, 15), is_recurring=True,
        )

    def child(self, first, last, *, phone='', status='active', branch=None, fresh=False):
        family = Family.objects.create(name=last, phone=phone, branch=branch or self.branch)
        Parent.objects.create(family=family, first_name='הורה', last_name=last, phone=phone, is_primary=True)
        child = Child.objects.create(
            family=family, first_name=first, last_name=last, birth_date=date(2018, 1, 1),
            gender='female', status=status,
        )
        if not fresh:
            # Registrations settled days ago; the in-flight guard is its own test.
            Child.objects.filter(pk=child.pk).update(created_at=timezone.now() - timedelta(days=3))
            child.refresh_from_db()
        return child

    def pay(self, child, lesson=None):
        payment = Payment.objects.create(
            child=child, family=child.family, lesson=lesson or self.lesson,
            base_amount=Decimal('235.00'), final_amount=Decimal('235.00'), status='completed',
        )
        RecurringPayment.objects.create(
            child=child, initial_payment=payment, amount=Decimal('235.00'), status='active',
            start_date=date.today(), next_billing_date=date.today(),
        )
        return payment

    def walk_in(self, first, last, *, phone='', on=None, lesson=None):
        enrollment, _ = create_ghost_enrollment(
            lesson=lesson or self.lesson, first_name=first, last_name=last,
            phone=phone, occurrence_date=on or self.past_thursday,
        )
        return enrollment.child

    def exists(self, child):
        return Child.objects.filter(pk=child.pk).exists()


class WalkInsFoldIntoTheRegisteredChild(MergeTestBase):
    def test_same_phone_and_first_name_folds_even_with_a_misspelt_surname(self):
        """ראם איזנקוביץ / ראם איזיקוביץ on one phone: one boy."""
        real = self.child('ראם', 'איזיקוביץ', phone='0504233614')
        LessonEnrollment.objects.create(lesson=self.lesson, child=real, status='active')
        self.pay(real)
        ghost = self.walk_in('ראם', 'איזנקוביץ', phone='050-423-3614')
        ghost_family = ghost.family

        resolve_duplicates()

        self.assertFalse(self.exists(ghost))
        self.assertFalse(Family.objects.filter(pk=ghost_family.pk).exists())
        mark = LessonAttendance.objects.get(lesson=self.lesson, occurrence_date=self.past_thursday)
        self.assertEqual(mark.child_id, real.pk)
        self.assertEqual(mark.status, 'present')

    def test_a_sibling_on_the_same_phone_is_left_alone(self):
        """לביא קגן and סיני קגן share their mother's phone. They are two children."""
        self.child('סיני', 'קגן', phone='0547841190', status='trial_signed')
        ghost = self.walk_in('לביא', 'קגן', phone='0547841190')
        resolve_duplicates()
        self.assertTrue(self.exists(ghost))

    def test_a_phoneless_walk_in_folds_by_a_name_unique_in_the_branch(self):
        real = self.child('עמית', 'ליברמן', phone='0547457600')
        ghost = self.walk_in('עמית', 'ליברמן')
        resolve_duplicates()
        self.assertFalse(self.exists(ghost))
        self.assertTrue(LessonAttendance.objects.filter(child=real, status='present').exists())

    def test_a_phoneless_walk_in_stays_when_two_children_share_the_name(self):
        self.child('מיקה', 'רובין', phone='0544557587')
        self.child('מיקה', 'רובין', phone='0544999268', status='trial_completed')
        ghost = self.walk_in('מיקה', 'רובין')
        resolve_duplicates()
        self.assertTrue(self.exists(ghost))

    def test_a_name_in_another_branch_does_not_count(self):
        self.child('צליל', 'דור', phone='0504080944', branch=self.other_branch)
        ghost = self.walk_in('צליל', 'דור')
        resolve_duplicates()
        self.assertTrue(self.exists(ghost))

    def test_a_walk_in_with_a_different_phone_stays(self):
        """A different number is evidence of a different child."""
        self.child('שחר', 'בר חיים', phone='0506357374')
        ghost = self.walk_in('שחר', 'בר חיים', phone='0507778481')
        resolve_duplicates()
        self.assertTrue(self.exists(ghost))

    def test_a_walk_in_marked_today_waits_until_the_day_is_over(self):
        """The instructor may still be tapping that row."""
        self.child('ארבל', 'אבידן', phone='0547699667')
        ghost = self.walk_in('ארבל', 'אבידן', on=date.today())
        result = resolve_duplicates()
        self.assertTrue(self.exists(ghost))
        self.assertEqual(result['merged'], 0)
        self.assertEqual(result['refused'], 1)

    def test_registering_the_real_child_takes_the_walk_in_at_once(self):
        ghost = self.walk_in('ליה', 'בסטקר', phone='0502425724')
        with self.captureOnCommitCallbacks(execute=True):
            self.child('ליה', 'בסטקר', phone='0502425724', status='pending', fresh=True)
        self.assertFalse(self.exists(ghost))


class RepeatRegistrationsFoldIntoThePayingOne(MergeTestBase):
    def test_trial_copies_fold_into_the_record_that_pays(self):
        """כרמי קלמר: four trial records and one paying one, same name and phone."""
        paying = self.child('כרמי', 'קלמר', phone='0544900523', status='active')
        LessonEnrollment.objects.create(lesson=self.other_lesson, child=paying, status='active')
        self.pay(paying, self.other_lesson)

        copy_same_lesson = self.child('כרמי', 'קלמר', phone='0544900523', status='trial_completed')
        LessonEnrollment.objects.create(
            lesson=self.other_lesson, child=copy_same_lesson, status='inactive',
            trial_lesson_date=self.past_thursday, trial_outcome='attended',
        )
        copy_other_lesson = self.child('כרמי', 'קלמר', phone='0544900523', status='trial_completed')
        LessonEnrollment.objects.create(
            lesson=self.lesson, child=copy_other_lesson, status='inactive',
            trial_lesson_date=self.past_thursday, trial_outcome='attended',
        )

        resolve_duplicates()

        self.assertEqual(Child.objects.filter(first_name='כרמי', last_name='קלמר').count(), 1)
        self.assertTrue(self.exists(paying))
        # The trial on a lesson the paying record lacked moved over whole.
        moved = LessonEnrollment.objects.get(child=paying, lesson=self.lesson)
        self.assertEqual(moved.trial_outcome, 'attended')
        # On the lesson both held, the paying row stayed and kept the trial's history.
        kept = LessonEnrollment.objects.get(child=paying, lesson=self.other_lesson)
        self.assertEqual(kept.status, 'active')
        self.assertEqual(kept.trial_held_on, self.past_thursday)

    def test_the_active_record_wins_when_none_holds_money(self):
        older = self.child('לני', 'גרינברג', phone='0549452526', status='trial_completed')
        newer_active = self.child('לני', 'גרינברג', phone='0549452526', status='active')
        resolve_duplicates()
        self.assertTrue(self.exists(newer_active))
        self.assertFalse(self.exists(older))


class MoneyIsNeverTouched(MergeTestBase):
    def test_two_records_that_both_hold_money_are_left_for_a_person(self):
        a = self.child('נויה', 'גד', phone='0501234567')
        b = self.child('נויה', 'גד', phone='0501234567')
        self.pay(a)
        self.pay(b)
        result = resolve_duplicates()
        self.assertTrue(self.exists(a))
        self.assertTrue(self.exists(b))
        self.assertEqual(result['skipped'], 1)

    def test_no_payment_or_standing_order_is_lost(self):
        paying = self.child('ניצן', 'חיימוביץ', phone='0509999999')
        self.pay(paying)
        self.child('ניצן', 'חיימוביץ', phone='0509999999', status='trial_completed')
        self.walk_in('ניצן', 'חיימוביץ', phone='0509999999')
        payments, standing = Payment.objects.count(), RecurringPayment.objects.count()

        resolve_duplicates()

        self.assertEqual(Payment.objects.count(), payments)
        self.assertEqual(RecurringPayment.objects.count(), standing)
        self.assertEqual(Child.objects.filter(first_name='ניצן').count(), 1)

    def test_a_dry_run_changes_nothing(self):
        self.child('יובל', 'שפר', phone='0505339149')
        ghost = self.walk_in('יובל', 'שפר', phone='0505339149')
        result = resolve_duplicates(dry_run=True)
        self.assertEqual(result['planned'], 1)
        self.assertEqual(result['merged'], 0)
        self.assertTrue(self.exists(ghost))


class ARegistrationInFlightIsLeftAlone(MergeTestBase):
    def test_a_record_saved_moments_ago_is_not_folded(self):
        """
        The widget saves the child before it charges the card. Folding that
        record into an older one would pull it out from under the payment.
        """
        self.child('שי', 'אסולין', phone='0502148215', status='active')
        in_flight = self.child('שי', 'אסולין', phone='0502148215', status='pending', fresh=True)
        result = resolve_duplicates()
        self.assertTrue(self.exists(in_flight))
        self.assertEqual(result['refused'], 1)
