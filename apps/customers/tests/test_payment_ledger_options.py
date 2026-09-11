"""
The payments tab's filter bar offers every course type, age group and
instructor in the range, not only what the page in hand holds.

The charges list is paginated (twenty a page), so the bar cannot read its
options off the rows it shows. The ledger answers them for the window when
asked — after the caller's scope and the dates, before any filter narrows it.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Payment

User = get_user_model()
LEDGER_URL = '/api/v1/customers/payments/ledger/'


class LedgerDimensionOptionsTest(APITestCase):
    def setUp(self):
        manager = TestDataFactory.create_user(username='manager-ledger-options@test')
        self.client.force_authenticate(User.objects.get(pk=manager.pk))
        self.family = TestDataFactory.create_family()
        self.child = TestDataFactory.create_child(family=self.family)
        # Two classes in two branches: ג׳ודו for 6–9 and שחייה for 10–12.
        self.judo = self._lesson('ג׳ודו', ages=(6, 9), instructor=('שירה', 'לוי'))
        self.swim = self._lesson('שחייה', ages=(10, 12), instructor=('אבי', 'כהן'))

    def _lesson(self, type_name, *, ages, instructor):
        course = TestDataFactory.create_course(
            name=f'{type_name} {ages[0]}–{ages[1]}',
            branch=TestDataFactory.create_branch(name=f'סניף {type_name}'),
            course_type=TestDataFactory.create_course_type(name=type_name),
            min_age=ages[0],
            max_age=ages[1],
        )
        first, last = instructor
        teacher = TestDataFactory.create_instructor(first_name=first, last_name=last, branch=course.branch)
        return TestDataFactory.create_lesson(course=course, instructor=teacher)

    def _charge(self, lesson, *, status='completed', days_ago=0):
        payment = Payment.objects.create(
            child=self.child, family=self.family, lesson=lesson, branch=lesson.course.branch,
            payment_type='one_time', status=status,
            base_amount=Decimal('100'), discount_amount=Decimal('0'), final_amount=Decimal('100'),
        )
        if days_ago:
            Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
        return payment

    def _options(self, **params):
        res = self.client.get(LEDGER_URL, {'with_options': '1', **params})
        self.assertEqual(res.status_code, 200, res.data)
        return res, res.data['dimension_options']

    @staticmethod
    def _values(options, key):
        return {option[key] for option in options if key in option}

    def test_a_class_from_age_zero_is_found_by_the_group_it_is_offered_as(self):
        # A 0–9 class is offered as '-9' ("עד גיל 9"). The filter used to look for
        # no minimum age at all, so choosing the group it was offered as found nothing.
        toddlers = self._lesson('פעוטות', ages=(0, 9), instructor=('דנה', 'בר'))
        self._charge(toddlers)
        self._charge(self.judo)

        res, options = self._options(age='-9')

        self.assertIn({'age_key': '-9', 'age_label': 'עד גיל 9'}, options)
        self.assertEqual(res.data['count'], 1)
        self.assertEqual(res.data['results'][0]['age_key'], '-9')

    def test_an_age_group_only_on_page_two_is_offered_on_page_one(self):
        for _ in range(20):
            self._charge(self.judo)
        self._charge(self.swim, days_ago=5)  # the oldest charge: last, so on page two

        res, options = self._options()

        self.assertEqual(res.data['count'], 21)
        self.assertEqual({row['age_key'] for row in res.data['results']}, {'6-9'})
        self.assertIn({'age_key': '10-12', 'age_label': 'גילאי 10–12'}, options)
        self.assertIn(
            {'course_type_id': str(self.swim.course.course_type_id), 'course_type_name': 'שחייה'}, options,
        )
        self.assertIn({'instructor_id': str(self.swim.instructor_id), 'instructor_name': 'אבי כהן'}, options)
        # Twenty charges of one class are one option of each kind, not twenty.
        self.assertEqual(sum(1 for option in options if option.get('age_key') == '6-9'), 1)

    def test_each_option_is_one_dimension_of_a_ledger_row(self):
        self._charge(self.judo)

        _, options = self._options()

        self.assertEqual({frozenset(option) for option in options}, {
            frozenset({'course_type_id', 'course_type_name'}),
            frozenset({'age_key', 'age_label'}),
            frozenset({'instructor_id', 'instructor_name'}),
        })

    def test_the_options_come_only_when_asked(self):
        self._charge(self.judo)

        res = self.client.get(LEDGER_URL)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn('dimension_options', res.data)

    def test_the_status_filter_narrows_the_rows_and_not_the_options(self):
        self._charge(self.judo, status='completed')
        self._charge(self.swim, status='failed')

        res, options = self._options(status='completed')

        self.assertEqual(res.data['count'], 1)
        self.assertEqual(self._values(options, 'age_key'), {'6-9', '10-12'})
        self.assertEqual(
            self._values(options, 'instructor_id'), {str(self.judo.instructor_id), str(self.swim.instructor_id)},
        )

    def test_a_choice_never_takes_away_its_own_alternatives(self):
        self._charge(self.judo)
        self._charge(self.swim)

        for params in (
            {'age': '6-9'},
            {'course_type': str(self.judo.course.course_type_id)},
            {'instructor': str(self.judo.instructor_id)},
            {'branch': str(self.judo.course.branch_id)},
            {'kind': 'trial'},
            {'search': 'אין כזה'},
        ):
            with self.subTest(**params):
                _, options = self._options(**params)
                self.assertEqual(self._values(options, 'age_key'), {'6-9', '10-12'})

    def test_the_options_hold_the_requested_window_only(self):
        self._charge(self.judo)
        self._charge(self.swim, days_ago=120)  # before the default 90 days

        _, options = self._options()
        self.assertEqual(self._values(options, 'age_key'), {'6-9'})

        _, wider = self._options(start_date=(timezone.localdate() - timedelta(days=150)).isoformat())
        self.assertEqual(self._values(wider, 'age_key'), {'6-9', '10-12'})

    def test_a_partner_is_offered_what_its_own_branches_hold(self):
        self._charge(self.judo)
        self._charge(self.swim)
        partner = TestDataFactory.create_user(
            username='partner-ledger-options@test', role=UserProfile.ROLE_PARTNER,
        )
        partner.profile.assigned_branches.add(self.judo.course.branch)
        self.client.force_authenticate(User.objects.get(pk=partner.pk))

        res, options = self._options()

        self.assertEqual(res.data['count'], 1)
        self.assertEqual(self._values(options, 'age_key'), {'6-9'})
        self.assertEqual(self._values(options, 'course_type_id'), {str(self.judo.course.course_type_id)})
        self.assertEqual(self._values(options, 'instructor_id'), {str(self.judo.instructor_id)})
