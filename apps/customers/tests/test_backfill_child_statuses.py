"""
backfill_child_statuses applies the status rule in bulk, so it keeps the
morning fix's protections (27.9.2026).

With --apply it used to rewrite every child the rule disagreed with. The rule
reads paid_until_date, and that date only moves when a charge lands: on the
first days of a month every subscriber whose charge has not landed yet looks
like someone whose money ran out. So a child someone is still charging is
never moved to לא פעיל or בעיה באשראי, and the full sweep is refused on the
1st–3rd unless it is asked for explicitly.
"""
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import RecurringPayment
from apps.enrollments.models import LessonEnrollment

TODAY = date.today()
LOCALDATE = 'apps.customers.management.commands.backfill_child_statuses.timezone.localdate'
MID_MONTH = date(2026, 10, 15)
SECOND_OF_THE_MONTH = date(2026, 10, 2)


def _child(status, **kwargs):
    family = TestDataFactory.create_family()
    TestDataFactory.create_parent(family=family)
    return TestDataFactory.create_child(family=family, status=status, **kwargs)


def _charged(child):
    RecurringPayment.objects.create(
        child=child, amount=Decimal('225'), status='active', tranzila_token='tok',
        start_date=TODAY - timedelta(days=90), next_billing_date=TODAY,
    )


def _run(*args, on=MID_MONTH):
    out = StringIO()
    with patch(LOCALDATE, return_value=on):
        call_command('backfill_child_statuses', *args, stdout=out)
    return out.getvalue()


class BackfillProtectionTests(TestCase):
    def test_the_full_sweep_without_apply_is_still_a_dry_run(self):
        child = _child('active', paid_until_date=TODAY - timedelta(days=20))
        output = _run('--all', on=SECOND_OF_THE_MONTH)
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')
        self.assertIn('Dry run', output)

    def test_the_full_sweep_is_refused_on_the_first_days_of_the_month(self):
        child = _child('active', paid_until_date=TODAY - timedelta(days=20))
        with self.assertRaises(CommandError):
            _run('--all', '--apply', on=SECOND_OF_THE_MONTH)
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')

    def test_the_refusal_can_be_overridden_when_it_is_meant(self):
        child = _child('active', paid_until_date=TODAY - timedelta(days=20))
        _run('--all', '--apply', '--allow-early-month', on=SECOND_OF_THE_MONTH)
        child.refresh_from_db()
        self.assertEqual(child.status, 'inactive')

    def test_a_child_with_nothing_left_is_moved_mid_month(self):
        child = _child('active', paid_until_date=TODAY - timedelta(days=20))
        _run('--all', '--apply')
        child.refresh_from_db()
        self.assertEqual(child.status, 'inactive')

    def test_a_child_still_charged_is_never_made_inactive(self):
        child = _child('active', paid_until_date=TODAY - timedelta(days=20))
        _charged(child)
        output = _run('--all', '--apply')
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')
        self.assertIn('still charged', output)

    def test_a_child_still_charged_is_never_made_a_card_problem(self):
        child = _child('active', paid_until_date=TODAY - timedelta(days=2))
        LessonEnrollment.objects.create(lesson=TestDataFactory.create_lesson(), child=child, status='active')
        _charged(child)
        _run('--all', '--apply')
        child.refresh_from_db()
        self.assertEqual(child.status, 'active')

    def test_a_child_paying_by_cheque_is_never_made_inactive(self):
        from apps.documents.models import CheckPlan

        child = _child('pending', paid_until_date=TODAY - timedelta(days=20))
        CheckPlan.objects.create(child=child, status='active')
        _run('--all', '--apply')
        child.refresh_from_db()
        # Money in, by the corrected rule — and in any case not לא פעיל.
        self.assertEqual(child.status, 'active')

    def test_a_retired_name_that_already_meant_a_card_problem_is_still_renamed(self):
        """not_paid → payment_problem is a new spelling, not a move."""
        child = _child('not_paid')
        _charged(child)
        _run('--apply')
        child.refresh_from_db()
        self.assertEqual(child.status, 'payment_problem')
