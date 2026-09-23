"""
Every read route in the system, called the way a manager's click would call it.

This is the weekly audit's sweep run against a fresh, fully migrated database
with a small but real set of rows: a family with a child in a lesson, a
payment, a standing order. Any route that answers with a server error or an
exception here is a button that would fail in front of the office.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.core.models import UserProfile
from apps.core.system_audit import AREAS, all_routes, area_for_day, israel_weekday, sweep_slice
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Payment, RecurringPayment


class RouteCoverageTests(TestCase):
    """The promise that no button goes unchecked, held as a rule."""

    def test_every_api_route_belongs_to_exactly_one_day(self):
        orphans = [r.template for r in all_routes() if not r.area]
        self.assertEqual(orphans, [], 'routes with no day would never be checked')

    def test_the_seven_days_each_have_an_area(self):
        self.assertEqual(sorted(area.day for area in AREAS), list(range(7)))

    def test_the_week_starts_on_sunday(self):
        self.assertEqual(israel_weekday(date(2026, 9, 20)), 0)   # Sunday
        self.assertEqual(israel_weekday(date(2026, 9, 26)), 6)   # Saturday
        self.assertEqual(area_for_day(date(2026, 9, 20)).key, 'billing')

    def test_routes_that_reach_outside_are_never_called_by_the_sweep(self):
        outside = [r for r in all_routes() if 'whatsapp' in r.template or 'cron' in r.template]
        self.assertTrue(outside)
        self.assertTrue(all(r.never_call for r in outside))


class SweepTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        manager = TestDataFactory.create_user(username='audit-manager@x.com', role=UserProfile.ROLE_MANAGER)
        UserProfile.objects.update_or_create(user=manager, defaults={'role': UserProfile.ROLE_MANAGER})
        family = TestDataFactory.create_family()
        TestDataFactory.create_parent(family=family)
        child = TestDataFactory.create_child(family=family, status='active')
        lesson = TestDataFactory.create_lesson()
        payment = Payment.objects.create(
            child=child, family=family, lesson=lesson,
            base_amount=Decimal('225'), final_amount=Decimal('225'), status='completed',
        )
        RecurringPayment.objects.create(
            child=child, initial_payment=payment, amount=Decimal('225'),
            status='active', tranzila_token='tok', start_date=date(2026, 9, 1),
            next_billing_date=date(2026, 10, 1),
        )

    def test_no_read_route_fails_in_any_area(self):
        failures = []
        called = 0
        for area in AREAS:
            result = sweep_slice(area.key, 0, budget_seconds=600)
            self.assertTrue(result.finished, area.key)
            for outcome in result.outcomes:
                if outcome.status is not None:
                    called += 1
                if outcome.error or (outcome.status and outcome.status >= 500):
                    failures.append(f'{area.key}: {outcome.path} → {outcome.status} {outcome.error}')
        self.assertEqual(failures, [], '\n'.join(failures))
        # A sweep that quietly skipped everything would also have "no failures".
        self.assertGreater(called, 120, f'only {called} routes were actually called')

    def test_the_sweep_keeps_nothing_it_did(self):
        """Every call runs inside a rolled-back transaction."""
        before = (Payment.objects.count(), RecurringPayment.objects.count())
        for area in AREAS:
            sweep_slice(area.key, 0, budget_seconds=600)
        self.assertEqual((Payment.objects.count(), RecurringPayment.objects.count()), before)

    def test_a_slice_stops_on_time_and_says_where_to_resume(self):
        result = sweep_slice('staff', 0, budget_seconds=0)
        self.assertFalse(result.finished)
        self.assertEqual(result.next_index, 0)
