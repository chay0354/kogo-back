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
        child = _child('ניסיון בתשלום', 'trial_signed')
        _paid(child, trial_lesson_date=TODAY)
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


class DashboardRefreshTests(TestCase):
    def test_this_months_counts_are_recalculated_and_never_locked(self):
        with patch('apps.instructors.utils.generate_monthly_snapshots', return_value={}) as generate:
            result = morning_fixes.refresh_dashboard_numbers()
        month = TODAY.strftime('%Y-%m')
        generate.assert_called_once_with(month, finalize=False)
        self.assertEqual(result['month'], month)


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
