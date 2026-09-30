"""
The small things the morning routine puts right by itself.

The owner asked not to be asked about these. So the line between "small" and
"not small" is what these tests hold: the transitions allowed, the ones never
made, the children never touched while someone is still charging them, and the
ceiling that stops a mistake in the rule from sweeping the whole list.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.core import morning_fixes
from apps.core.morning_fixes import fix_child_statuses, status_fix_candidates
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Child, Payment, RecurringPayment
from apps.customers.status_history_models import ChildStatusHistory

TODAY = date.today()


def _child(name, status, **kwargs):
    family = TestDataFactory.create_family()
    TestDataFactory.create_parent(family=family)
    return TestDataFactory.create_child(family=family, first_name=name, status=status, **kwargs)


def _paid(child, **kwargs):
    # Money for a course: since 27.9.2026 a completed payment with no lesson is
    # not a registration paid for, and these tests are about one that is.
    kwargs.setdefault('lesson', TestDataFactory.create_lesson())
    return Payment.objects.create(
        child=child, family=child.family, base_amount=Decimal('225'),
        final_amount=Decimal('225'), status='completed', **kwargs,
    )


class StatusFixTests(TestCase):
    def test_a_trial_child_who_paid_becomes_active_and_the_change_is_recorded(self):
        child = _child('שילם', 'trial_completed')
        _paid(child)
        result = fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')
        self.assertEqual(len(result['applied']), 1)
        history = ChildStatusHistory.objects.get(child=child)
        self.assertEqual((history.previous_status, history.new_status), ('trial_completed', 'active'))
        self.assertIn('אוטומטית', history.reason)

    def test_a_paid_trial_is_not_a_registration(self):
        """A parent who paid for one trial lesson has not joined the course."""
        from apps.enrollments.models import LessonEnrollment

        child = _child('ניסיון בתשלום', 'trial_signed')
        payment = _paid(child, trial_lesson_date=TODAY)
        # A paid trial that went through always has its booked row (the widget
        # writes it with the charge). Without it the child has no trial at all,
        # which since 27.9.2026 the morning reads as בתהליך רישום.
        LessonEnrollment.objects.create(
            lesson=payment.lesson, child=child, status='active', start_date=TODAY, trial_lesson_date=TODAY,
        )
        fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'trial_signed')

    def test_a_child_whose_paid_period_ended_becomes_inactive(self):
        child = _child('סיים', 'active', paid_until_date=TODAY - timedelta(days=20))
        fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'inactive')

    def test_a_child_someone_is_still_charging_is_never_made_inactive(self):
        child = _child('עדיין בקבע', 'active', paid_until_date=TODAY - timedelta(days=20))
        RecurringPayment.objects.create(
            child=child, amount=Decimal('225'), status='active', tranzila_token='tok',
            start_date=TODAY - timedelta(days=90), next_billing_date=TODAY,
        )
        fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')

    def test_a_child_paying_in_cash_is_never_made_inactive(self):
        from apps.documents.models import CashPlan

        child = _child('מזומן', 'active', paid_until_date=TODAY - timedelta(days=20))
        CashPlan.objects.create(
            child=child, lesson=TestDataFactory.create_lesson(), status='active',
            total_amount=Decimal('2250'), monthly_amount=Decimal('225'),
        )
        fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')

    def test_nothing_is_ever_moved_to_payment_problem(self):
        """Billing sets that one, and it starts the card-update messages."""
        allowed_targets = {target for _, target in morning_fixes.AUTO_TRANSITIONS}
        self.assertNotIn('payment_problem', allowed_targets)

    def test_a_change_the_office_made_a_moment_ago_is_left_alone(self):
        child = _child('שונה עכשיו', 'trial_completed')
        _paid(child)
        candidates = status_fix_candidates()
        Child.objects.filter(pk=child.pk).update(status='inactive')
        with patch.object(morning_fixes, 'status_fix_candidates', return_value=candidates):
            result = fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'inactive')
        self.assertEqual(result['applied'], [])

    def test_one_morning_moves_no_more_than_the_ceiling(self):
        for index in range(5):
            _paid(_child(f'ילד {index}', 'pending'))
        with patch.object(morning_fixes, 'MAX_STATUS_FIXES_PER_MORNING', 3):
            result = fix_child_statuses()
        self.assertEqual(len(result['applied']), 3)
        self.assertEqual(result['waiting'], 2)
        self.assertEqual(Child.objects.filter(status='active').count(), 3)

    def test_a_ghost_is_never_touched(self):
        child = _child('רפאים', 'ghost')
        _paid(child)
        fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'ghost')


class TrialStatusFixTests(TestCase):
    """
    The trial statuses follow the trial rows (27.9.2026): a cancelled trial is
    not one ahead, and a new one booked ahead brings a child back to
    נרשם לניסיון, which is what the day's reminders read.
    """

    def trial(self, child, *, days_ahead, status='active', **kwargs):
        from apps.enrollments.models import LessonEnrollment

        when = TODAY + timedelta(days=days_ahead)
        return LessonEnrollment.objects.create(
            lesson=TestDataFactory.create_lesson(), child=child, status=status,
            start_date=when, trial_lesson_date=when, **kwargs,
        )

    def test_a_child_whose_trial_was_cancelled_becomes_inactive_and_it_is_recorded(self):
        child = _child('בוטל', 'trial_signed')
        self.trial(child, days_ahead=4, status='inactive', end_date=TODAY)
        fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'inactive')
        history = ChildStatusHistory.objects.get(child=child)
        self.assertEqual((history.previous_status, history.new_status), ('trial_signed', 'inactive'))

    def test_a_cancelled_trial_beside_an_unpaid_sign_up_is_back_in_registration(self):
        from apps.enrollments.models import LessonEnrollment

        child = _child('נרשם ולא שילם', 'trial_signed')
        self.trial(child, days_ahead=4, status='inactive', end_date=TODAY)
        LessonEnrollment.objects.create(lesson=TestDataFactory.create_lesson(), child=child, status='active')
        fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'pending')

    def test_a_cancelled_repeat_trial_leaves_the_first_one_that_took_place(self):
        child = _child('ניסיון שני', 'trial_signed')
        self.trial(child, days_ahead=-7, status='inactive', trial_outcome='attended', end_date=TODAY - timedelta(days=7))
        self.trial(child, days_ahead=3, status='inactive', end_date=TODAY)
        fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'trial_completed')

    def test_a_completed_trial_child_with_a_new_trial_ahead_is_signed_again(self):
        child = _child('ניסיון נוסף', 'trial_completed')
        self.trial(child, days_ahead=2)
        fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'trial_signed')

    def test_a_trial_child_someone_is_still_charging_is_never_made_inactive(self):
        child = _child('בקבע בלי תשלום', 'trial_signed')
        self.trial(child, days_ahead=4, status='inactive', end_date=TODAY)
        RecurringPayment.objects.create(
            child=child, amount=Decimal('225'), status='active', tranzila_token='tok',
            start_date=TODAY, next_billing_date=TODAY + timedelta(days=5),
        )
        fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'trial_signed')

    def test_a_declined_card_is_left_for_billing_to_flag(self):
        """The rule reads בעיה באשראי, but the morning never moves anyone there."""
        from apps.enrollments.models import LessonEnrollment

        child = _child('כרטיס נדחה', 'active')
        lesson = TestDataFactory.create_lesson()
        LessonEnrollment.objects.create(lesson=lesson, child=child, status='active')
        Payment.objects.create(
            child=child, family=child.family, lesson=lesson, status='completed',
            base_amount=Decimal('225'), final_amount=Decimal('120'), registration_fee=Decimal('120'),
        )
        RecurringPayment.objects.create(
            child=child, amount=Decimal('225'), status='failed', tranzila_token='tok',
            start_date=TODAY, next_billing_date=TODAY,
        )
        fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')


class StatusFixInSlicesTests(TestCase):
    """
    Three thousand children took about 400 seconds to go through — longer than
    one request may run — so the morning goes through them in slices.
    """

    def test_when_the_time_is_up_it_stops_and_says_where(self):
        _paid(_child('שילם', 'pending'))
        result = fix_child_statuses(budget_seconds=-1)
        self.assertFalse(result['finished'])
        self.assertEqual(result['applied'], [])

    def test_a_slice_carries_on_after_the_last_child_it_reached(self):
        children = sorted((_child(f'ילד {i}', 'pending') for i in range(2)), key=lambda c: str(c.id))
        for child in children:
            _paid(child)
        result = fix_child_statuses(after_id=str(children[0].id))
        self.assertTrue(result['finished'])
        self.assertEqual([change['child_id'] for change in result['applied']], [str(children[1].id)])
        children[0].refresh_from_db()
        self.assertEqual(children[0].status, 'pending')

    def test_the_ceiling_covers_the_whole_morning_not_each_slice(self):
        _paid(_child('שילם', 'pending'))
        result = fix_child_statuses(already_applied=morning_fixes.MAX_STATUS_FIXES_PER_MORNING)
        self.assertEqual(result['applied'], [])
        self.assertEqual(result['waiting'], 1)

    def test_the_brief_line_carries_on_from_this_mornings_last_slice(self):
        from apps.core.daily_brief import check_fix_child_statuses
        from apps.core.daily_brief_views import merge_into_today

        children = sorted((_child(f'ילד {i}', 'pending') for i in range(2)), key=lambda c: str(c.id))
        for child in children:
            _paid(child)
        earlier = {'child_id': 'x', 'name': 'תוקן קודם', 'from': 'ממתין', 'to': 'פעיל'}
        merge_into_today({
            'key': 'fix_child_statuses', 'title': '', 'severity': 'yellow', 'count': 1,
            'summary': '', 'action': '', 'rows': [], 'duration_ms': 0, 'continues': True,
            'progress': {'after_id': str(children[0].id), 'applied': [earlier], 'waiting': 0},
        })
        item = check_fix_child_statuses(TODAY)
        self.assertFalse(item.continues)
        self.assertEqual(item.count, 2)
        self.assertEqual([row['label'] for row in item.rows], ['תוקן קודם', children[1].full_name])

    def test_an_unfinished_slice_says_it_will_carry_on(self):
        from apps.core.daily_brief import check_fix_child_statuses

        _paid(_child('שילם', 'pending'))
        with patch('apps.core.daily_brief.RESUMABLE_SLICE_SECONDS', -1):
            item = check_fix_child_statuses(TODAY)
        self.assertTrue(item.continues)
        self.assertIn('ממשיך', item.summary)


class DashboardRefreshTests(TestCase):
    def test_this_months_counts_are_recalculated_in_slices(self):
        with patch('apps.instructors.utils.refresh_month_snapshots', return_value={'finished': True}) as refresh:
            morning_fixes.refresh_dashboard_numbers(budget_seconds=30)
        refresh.assert_called_once_with(TODAY.strftime('%Y-%m'), budget_seconds=30)

    def test_an_unfinished_recount_says_how_far_it_got(self):
        from apps.core.daily_brief import check_refresh_dashboard

        progress = {
            'month': TODAY.strftime('%Y-%m'), 'finished': False,
            'lessons_done': 40, 'lessons_total': 236, 'instructors_done': 0, 'instructors_total': 32,
        }
        with patch('apps.instructors.utils.refresh_month_snapshots', return_value=progress):
            item = check_refresh_dashboard(TODAY)
        self.assertTrue(item.continues)
        self.assertIn('40 מתוך 236', item.summary)

    def test_a_finished_recount_is_a_quiet_line(self):
        from apps.core.daily_brief import GREEN, check_refresh_dashboard

        done = {
            'month': TODAY.strftime('%Y-%m'), 'finished': True,
            'lessons_done': 1, 'lessons_total': 1, 'instructors_done': 1, 'instructors_total': 1,
        }
        with patch('apps.instructors.utils.refresh_month_snapshots', return_value=done):
            item = check_refresh_dashboard(TODAY)
        self.assertFalse(item.continues)
        self.assertEqual(item.severity, GREEN)


class MorningSlicesTests(TestCase):
    def test_an_unfinished_morning_fix_is_run_again_on_the_next_call(self):
        from apps.core import daily_brief_views
        from apps.core.daily_brief import check_catalogue

        for entry in check_catalogue():
            daily_brief_views.merge_into_today({
                'key': entry['key'], 'title': '', 'severity': 'green', 'count': 0, 'summary': '',
                'action': '', 'rows': [], 'duration_ms': 0,
                'continues': entry['key'] == 'refresh_dashboard',
            })
        answer = {
            'key': 'refresh_dashboard', 'title': '', 'severity': 'green', 'count': 0, 'summary': '',
            'action': '', 'rows': [], 'duration_ms': 0, 'continues': False,
        }
        with patch.object(daily_brief_views, 'run_check', return_value=answer) as run_check:
            daily_brief_views.run_pending_checks(budget_seconds=60)
            run_check.assert_called_once_with('refresh_dashboard')
            run_check.reset_mock()
            daily_brief_views.run_pending_checks(budget_seconds=60)
            run_check.assert_not_called()

    def test_the_status_fix_goes_before_the_recount(self):
        """The dashboard counts students by their status, so statuses are put right first."""
        from apps.core import daily_brief_views

        answer = {
            'key': 'x', 'title': '', 'severity': 'green', 'count': 0, 'summary': '',
            'action': '', 'rows': [], 'duration_ms': 0, 'continues': True,
        }
        with patch.object(daily_brief_views, 'run_check', return_value=answer) as run_check:
            daily_brief_views.run_pending_checks(budget_seconds=-1)
        run_check.assert_called_once_with('fix_child_statuses')


class BriefLinesTests(TestCase):
    def test_the_brief_lists_what_was_fixed(self):
        from apps.core.daily_brief import check_fix_child_statuses

        child = _child('תוקן', 'pending')
        _paid(child)
        item = check_fix_child_statuses(TODAY)
        self.assertEqual(item.count, 1)
        self.assertIn('תוקן', item.rows[0]['label'])
        self.assertIn('←', item.rows[0]['detail'])

    def test_a_quiet_morning_says_there_was_nothing_to_fix(self):
        from apps.core.daily_brief import check_fix_child_statuses

        self.assertIn('לא היה', check_fix_child_statuses(TODAY).summary)

    def test_an_open_previous_month_is_reported_and_not_closed(self):
        from apps.core.daily_brief import check_monthly_finalization
        from apps.core.models import InstructorMonthlySnapshot

        instructor = TestDataFactory.create_instructor()
        previous = (TODAY.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
        InstructorMonthlySnapshot.objects.create(instructor=instructor, month=previous, is_finalized=False)
        item = check_monthly_finalization(TODAY)
        self.assertEqual(item.count, 1)
        self.assertFalse(InstructorMonthlySnapshot.objects.get(month=previous).is_finalized)


class StatusHistoryOnceTests(TestCase):
    """One change, one row in the child's history — the one that says why."""

    def test_a_morning_fix_to_inactive_is_written_once_with_its_reason(self):
        child = _child('סיים', 'active', paid_until_date=TODAY - timedelta(days=20))
        fix_child_statuses()
        rows = ChildStatusHistory.objects.filter(child=child)
        self.assertEqual(rows.count(), 1)
        self.assertIn('אוטומטית', rows.get().reason)

    def test_the_office_leaving_a_child_by_hand_is_still_recorded(self):
        child = _child('עזב', 'payment_problem')
        child.status = 'inactive'
        child.save(update_fields=['status', 'updated_at'])
        self.assertEqual(ChildStatusHistory.objects.filter(child=child, new_status='inactive').count(), 1)


class LeftForAPersonTests(TestCase):
    """
    A child the rule disagrees with, whom the morning may not move by itself,
    used to be passed over without a word (30.9.2026). Now the brief names them.
    """

    def _declined_card_child(self):
        from apps.enrollments.models import LessonEnrollment

        child = _child('כרטיס נדחה', 'active')
        lesson = TestDataFactory.create_lesson()
        LessonEnrollment.objects.create(lesson=lesson, child=child, status='active')
        Payment.objects.create(
            child=child, family=child.family, lesson=lesson, status='completed',
            base_amount=Decimal('225'), final_amount=Decimal('120'), registration_fee=Decimal('120'),
        )
        RecurringPayment.objects.create(
            child=child, amount=Decimal('225'), status='failed', tranzila_token='tok',
            start_date=TODAY, next_billing_date=TODAY,
        )
        return child

    def test_a_move_the_morning_may_not_make_is_named_and_not_made(self):
        child = self._declined_card_child()
        result = fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')
        self.assertEqual(result['applied'], [])
        [left] = result['needs_person']
        self.assertEqual(left['child_id'], str(child.id))
        self.assertEqual(left['why'], morning_fixes.LEFT_NOT_AUTOMATIC)
        self.assertEqual((left['from'], left['to']), ('פעיל', 'בעיה באשראי'))

    def test_a_child_still_being_charged_is_named_not_made_inactive(self):
        child = _child('עדיין בקבע', 'active', paid_until_date=TODAY - timedelta(days=20))
        RecurringPayment.objects.create(
            child=child, amount=Decimal('225'), status='active', tranzila_token='tok',
            start_date=TODAY - timedelta(days=90), next_billing_date=TODAY,
        )
        result = fix_child_statuses()
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')
        [left] = result['needs_person']
        self.assertEqual(left['why'], morning_fixes.LEFT_STILL_CHARGED)

    def test_a_child_whose_status_agrees_is_not_named(self):
        child = _child('משלם', 'active')
        _paid(child)
        self.assertEqual(fix_child_statuses()['needs_person'], [])

    def test_the_brief_puts_them_first_and_asks_for_a_person(self):
        from apps.core.daily_brief import check_fix_child_statuses

        left = self._declined_card_child()
        fixed = _child('שילם', 'pending')
        _paid(fixed)
        item = check_fix_child_statuses(TODAY)
        self.assertEqual(item.severity, 'yellow')
        self.assertEqual(item.count, 2)
        self.assertEqual([row['label'] for row in item.rows], [left.full_name, fixed.full_name])
        self.assertIn('בעיה באשראי', item.rows[0]['detail'])
        self.assertIn('צריך שמישהו יחליט', item.summary)

    def test_the_names_carry_over_from_this_mornings_earlier_slices(self):
        from apps.core.daily_brief import check_fix_child_statuses
        from apps.core.daily_brief_views import merge_into_today

        earlier = {'child_id': 'x', 'name': 'מבוקר מוקדם', 'from': 'פעיל', 'to': 'ממתין', 'why': 'not_automatic'}
        merge_into_today({
            'key': 'fix_child_statuses', 'title': '', 'severity': 'yellow', 'count': 0,
            'summary': '', 'action': '', 'rows': [], 'duration_ms': 0, 'continues': True,
            'progress': {'after_id': None, 'applied': [], 'needs_person': [earlier], 'waiting': 0},
        })
        item = check_fix_child_statuses(TODAY)
        self.assertFalse(item.continues)
        self.assertEqual([row['label'] for row in item.rows], ['מבוקר מוקדם'])
        self.assertEqual(item.severity, 'yellow')
