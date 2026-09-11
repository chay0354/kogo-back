"""Card links for a twice/thrice-a-week track, and where the link points.

A child on two days of a course is on one standing order at the track's
combined price — the widget has always billed it that way, and a card link has
to agree with it, or the office sends a parent a price they never saw.
"""
from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

from django.test import RequestFactory, override_settings

from apps.core.frontend_url import public_frontend_url
from apps.courses.models import Lesson, LessonBundle
from apps.customers.card_link import CardLinkError, apply_card_link, quote_standing_order
from apps.customers.models import Child, Payment, RecurringPayment
from apps.customers.tests.test_card_link import CARD, OK_CHARGE, _Base
from apps.enrollments.models import LessonEnrollment
from apps.payment_links.models import CardLink


class _BundleBase(_Base):
    def setUp(self):
        super().setUp()
        self.lesson_tue = Lesson.objects.create(
            course=self.course, room=self.lesson.room, day_of_week=2,
            start_time=time(17, 0), end_time=time(18, 0), is_recurring=True,
        )
        self.bundle = LessonBundle.objects.create(course=self.course, combined_price=Decimal('600.00'), is_active=True)
        self.bundle.lessons.set([self.lesson, self.lesson_tue])

    def _enroll(self, lesson, *, bundle=None, child=None):
        return LessonEnrollment.objects.create(
            child=child or self.child, lesson=lesson, status='active', start_date=date.today(), bundle=bundle,
        )

    def _bundle_link(self):
        return CardLink.objects.create(
            kind=CardLink.KIND_STANDING_ORDER, child=self.child, lesson=self.lesson, bundle=self.bundle,
            branch=self.branch, created_by=self.manager,
        )


class BundleQuoteTest(_BundleBase):
    def test_a_track_is_billed_at_its_combined_price(self):
        quote = quote_standing_order(self._bundle_link())

        self.assertEqual(quote['monthly_amount'], Decimal('600.00'))
        self.assertEqual(quote['bundle'], self.bundle)

    def test_a_full_class_does_not_refuse_the_child_already_in_it(self):
        self.course.capacity = 1
        self.course.save(update_fields=['capacity'])
        self._enroll(self.lesson, bundle=self.bundle)
        self._enroll(self.lesson_tue, bundle=self.bundle)

        quote = quote_standing_order(self._bundle_link())

        self.assertEqual(quote['monthly_amount'], Decimal('600.00'))

    def test_a_full_class_still_refuses_a_new_child(self):
        self.course.capacity = 1
        self.course.save(update_fields=['capacity'])
        self._enroll(self.lesson, bundle=self.bundle)
        self._enroll(self.lesson_tue, bundle=self.bundle)
        newcomer = Child.objects.create(
            family=self.family, first_name='Tal', last_name='Cohen', birth_date=date(2017, 1, 1),
            gender='male', status='active',
        )
        link = CardLink.objects.create(
            kind=CardLink.KIND_STANDING_ORDER, child=newcomer, lesson=self.lesson, bundle=self.bundle,
            branch=self.branch, created_by=self.manager,
        )

        with self.assertRaises(CardLinkError):
            quote_standing_order(link)


class BundleOptionsApiTest(_BundleBase):
    def _options(self):
        res = self.client.get('/api/v1/customers/card-links/options/', {'child_id': str(self.child.id)})
        self.assertEqual(res.status_code, 200)
        return res.data['options']

    def test_a_child_on_a_track_is_offered_the_track_as_one_unit(self):
        self._enroll(self.lesson, bundle=self.bundle)
        self._enroll(self.lesson_tue, bundle=self.bundle)

        options = self._options()

        self.assertEqual(len(options), 1, options)
        track = options[0]
        self.assertEqual(track['kind'], 'bundle')
        self.assertTrue(track['enrolled'])
        self.assertEqual(track['frequency_label'], 'פעמיים בשבוע')
        self.assertEqual([s['lesson_id'] for s in track['sessions']], [str(self.lesson.id), str(self.lesson_tue.id)])
        self.assertEqual(track['quote']['monthly_amount'], '600.00')

    def test_an_older_signup_on_every_day_is_offered_the_track_first(self):
        self._enroll(self.lesson)
        self._enroll(self.lesson_tue)

        options = self._options()

        self.assertEqual(options[0]['kind'], 'bundle')
        self.assertTrue(options[0]['enrolled'])
        self.assertEqual({o['kind'] for o in options[1:]}, {'lesson'})

    def test_a_once_a_week_child_is_offered_the_track_as_an_upgrade(self):
        self._enroll(self.lesson)

        options = self._options()

        self.assertEqual(options[0]['kind'], 'lesson')
        self.assertTrue(options[0]['enrolled'])
        upgrade = options[1]
        self.assertEqual(upgrade['kind'], 'bundle')
        self.assertFalse(upgrade['enrolled'])

    def test_a_unit_already_billed_is_marked_and_not_priced(self):
        self._enroll(self.lesson, bundle=self.bundle)
        self._enroll(self.lesson_tue, bundle=self.bundle)
        payment = Payment.objects.create(
            child=self.child, family=self.family, lesson=self.lesson, bundle=self.bundle,
            payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('600.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('600.00'),
        )
        RecurringPayment.objects.create(
            child=self.child, initial_payment=payment, tranzila_token='tok', status='active',
            amount=Decimal('600.00'), billing_day=1, start_date=date.today(),
        )

        track = self._options()[0]

        self.assertTrue(track['has_standing_order'])
        self.assertNotIn('quote', track)


class BundleCreateAndChargeTest(_BundleBase):
    def test_creating_a_link_for_a_track_hangs_it_on_the_first_day(self):
        res = self.client.post('/api/v1/customers/card-links/', {
            'kind': 'standing_order', 'child_id': str(self.child.id),
            'bundle_id': str(self.bundle.id), 'include_registration_fee': True,
        }, format='json')

        self.assertEqual(res.status_code, 201, res.data)
        link = CardLink.objects.get(id=res.data['id'])
        self.assertEqual(link.bundle, self.bundle)
        self.assertEqual(link.lesson, self.lesson)
        self.assertIn('/c/', res.data['public_url'])
        self.assertIn('פעמיים בשבוע', res.data['lesson_label'])
        self.assertEqual(res.data['quote']['monthly_amount'], '600.00')

    def test_paying_a_track_link_opens_one_standing_order_for_every_day(self):
        link = self._bundle_link()
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            prod.return_value.charge_with_card.return_value = OK_CHARGE
            prod.return_value.verify_card.return_value = OK_CHARGE
            result = apply_card_link(link, CARD)

        self.assertTrue(result['success'], result)
        link.refresh_from_db()
        self.assertEqual(link.status, CardLink.STATUS_COMPLETED)
        self.assertEqual(link.payment.bundle, self.bundle)
        recurring = RecurringPayment.objects.get(initial_payment=link.payment)
        self.assertEqual(recurring.amount, Decimal('600.00'))
        enrolled = set(
            LessonEnrollment.objects.filter(child=self.child, status='active').values_list('lesson_id', flat=True)
        )
        self.assertEqual(enrolled, {self.lesson.id, self.lesson_tue.id})

    def test_the_parent_page_lists_every_day_of_the_track(self):
        link = self._bundle_link()

        res = self.client.get(f'/api/v1/customers/card-link/{link.token}/')

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['frequency_label'], 'פעמיים בשבוע')
        self.assertEqual(len(res.data['sessions']), 2)


class PublicUrlTest(_Base):
    """A missing CRM_FRONTEND_URL used to send parents to http://localhost:3000."""

    @override_settings(
        CRM_FRONTEND_URL='',
        CORS_ALLOWED_ORIGINS=['http://localhost:3000', 'https://crm.cogo.co.il'],
        CORS_ALLOWED_ORIGIN_REGEXES=[r'^https://[\w.-]+\.vercel\.app$'],
    )
    def test_the_crm_origin_the_office_works_from_becomes_the_link_host(self):
        request = RequestFactory().post('/', HTTP_ORIGIN='https://crm.cogo.co.il')
        self.assertEqual(public_frontend_url(request), 'https://crm.cogo.co.il')

        preview = RequestFactory().post('/', HTTP_ORIGIN='https://kogo-front-abc.vercel.app')
        self.assertEqual(public_frontend_url(preview), 'https://kogo-front-abc.vercel.app')

    @override_settings(
        CRM_FRONTEND_URL='',
        CORS_ALLOWED_ORIGINS=['http://localhost:3000', 'https://crm.cogo.co.il'],
        CORS_ALLOWED_ORIGIN_REGEXES=[],
    )
    def test_an_origin_the_api_does_not_trust_is_ignored(self):
        request = RequestFactory().post('/', HTTP_ORIGIN='https://evil.example')

        self.assertNotEqual(public_frontend_url(request), 'https://evil.example')

    @override_settings(CRM_FRONTEND_URL='https://crm.cogo.co.il')
    def test_an_explicit_setting_always_wins(self):
        request = RequestFactory().post('/', HTTP_ORIGIN='http://localhost:3100')

        self.assertEqual(public_frontend_url(request), 'https://crm.cogo.co.il')

    def test_the_short_link_is_short(self):
        link = self._sto_link()
        res = self.client.get('/api/v1/customers/card-links/', {'child_id': str(self.child.id)})

        url = res.data[0]['public_url']
        self.assertIn('/c/', url)
        self.assertEqual(len(url.rsplit('/', 1)[1]), 10)
        self.assertEqual(url.rsplit('/', 1)[1], CardLink.objects.get(id=link.id).token)
