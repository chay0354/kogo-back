"""One child's monthly payment for one class split between cards (owner, 15.10.2026).

Parents who live apart each pay a part: ₪260 a month is ₪130 on each card,
and the registration fee is divided too. Made from the CRM only; each payer
pays through their own card link, is charged every month on their own card,
and gets their own receipt.
"""
from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import Branch, Room, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.card_link import CardLinkError, apply_card_link, preview_payload, quote_standing_order, send_card_link_whatsapp
from apps.customers.financial_models import Invoice
from apps.customers.models import Child, Family, Parent, Payment, RecurringPayment
from apps.customers.payment_split import SplitError, divide
from apps.customers.problem_flags import (
    DOUBLE_CHARGE,
    DOUBLE_STANDING_ORDER,
    SPLIT_INCOMPLETE,
    problems_for_children,
)
from apps.customers.recurring_billing import process_due_recurring_charges
from apps.customers.split_models import PaymentSplit, PaymentSplitShare
from apps.customers.subscription_invoice_email import invoice_recipients
from apps.customers.tests.test_card_link import CARD, OK_CHARGE, _client_for, _user
from apps.enrollments.models import LessonEnrollment
from apps.payment_links.models import CardLink

SPLITS = '/api/v1/customers/payment-splits/'


def _codes(child):
    return [problem.code for problem in problems_for_children([child])[child.id]]


class DivideTests(TestCase):
    def test_equal_halves(self):
        self.assertEqual(divide(Decimal('260'), 2), [Decimal('130.00'), Decimal('130.00')])

    def test_thirds_add_up_to_the_agora(self):
        shares = divide(Decimal('335'), 3)
        self.assertEqual(shares, [Decimal('111.67'), Decimal('111.67'), Decimal('111.66')])
        self.assertEqual(sum(shares), Decimal('335.00'))

    def test_the_office_may_set_its_own_figures_if_they_add_up(self):
        self.assertEqual(divide(Decimal('260'), 2, ['160', '100']), [Decimal('160.00'), Decimal('100.00')])
        with self.assertRaises(SplitError):
            divide(Decimal('260'), 2, ['160', '90'])
        with self.assertRaises(SplitError):
            divide(Decimal('260'), 2, ['259.50', '0.50'])

    def test_two_to_three_payers(self):
        for parts in (1, 4):
            with self.assertRaises(SplitError):
                divide(Decimal('260'), parts)


@override_settings(REGISTRATION_FEE_ILS=120, SUBSCRIPTION_FIRST_CHARGE_DATE='')
class _Base(TestCase):
    def setUp(self):
        cache.clear()
        self.branch = Branch.objects.create(name='ראש העין')
        room = Room.objects.create(branch=self.branch, name='A', capacity=20)
        ctype = CourseType.objects.create(name='קפוארה')
        self.course = Course.objects.create(course_type=ctype, name='קפוארה', price=Decimal('260.00'), capacity=12, branch=self.branch)
        self.lesson = Lesson.objects.create(
            course=self.course, room=room, day_of_week=0, start_time=time(17, 0), end_time=time(18, 0), is_recurring=True,
        )
        self.family = Family.objects.create(name='לוי', phone='0501111111', email='family@test.com', branch=self.branch)
        self.mother = Parent.objects.create(
            family=self.family, first_name='דנה', last_name='לוי', phone='0501111111', email='dana@test.com', is_primary=True,
        )
        self.child = Child.objects.create(
            family=self.family, first_name='נועה', last_name='לוי', birth_date=date(2016, 1, 1), gender='female', status='trial_signed',
        )
        self.manager = _user(UserProfile.ROLE_MANAGER, 'm@test.com')
        self.client = _client_for(self.manager)

    def _make(self, **extra):
        body = {
            'child_id': str(self.child.id), 'lesson_id': str(self.lesson.id),
            'payers': [
                {'parent_id': str(self.mother.id)},
                {'name': 'אבי כהן', 'phone': '0502222222', 'email': 'avi@test.com'},
            ],
        }
        body.update(extra)
        return self.client.post(SPLITS, body, format='json')

    def _links(self):
        return list(CardLink.objects.filter(split_share__isnull=False).order_by('split_share__position'))

    def _pay(self, link, token='tok_abc'):
        with patch('apps.customers.card_link.TranzilaService.production') as prod:
            prod.return_value.charge_with_card.return_value = dict(OK_CHARGE, token=token)
            prod.return_value.verify_card.return_value = dict(OK_CHARGE, token=token)
            out = apply_card_link(CardLink.objects.get(id=link.id), CARD)
        self.assertTrue(out['success'], out)
        return out


class MakingASplitTests(_Base):
    def test_preview_shows_the_whole_and_each_part(self):
        res = self.client.post(f'{SPLITS}preview/', {
            'child_id': str(self.child.id), 'lesson_id': str(self.lesson.id), 'parts': 2,
        }, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data['monthly_total'], '260.00')
        self.assertEqual(res.data['registration_fee_total'], '120.00')
        self.assertEqual(res.data['shares'], [
            {'monthly_amount': '130.00', 'registration_fee': '60.00'},
            {'monthly_amount': '130.00', 'registration_fee': '60.00'},
        ])
        self.assertFalse(PaymentSplit.objects.exists())

    def test_a_link_per_payer_each_billing_its_part(self):
        res = self._make()
        self.assertEqual(res.status_code, 201, res.content)
        links = self._links()
        self.assertEqual(len(links), 2)
        self.assertEqual([row['split']['payer_name'] for row in res.data['links']], ['דנה לוי', 'אבי כהן'])
        for row in res.data['links']:
            self.assertEqual(row['quote']['monthly_amount'], '130.00')
            self.assertEqual(row['quote']['registration_fee'], '60.00')
        first = quote_standing_order(links[0])
        whole_prorated = quote_standing_order(CardLink(kind='standing_order', child=self.child, lesson=self.lesson))['prorated_lesson']
        self.assertLessEqual(abs(first['prorated_lesson'] * 2 - whole_prorated), Decimal('0.02'))
        self.assertEqual(first['first_charge'], first['prorated_lesson'] + Decimal('60.00'))
        self.assertIn('חלק 1 מתוך 2', first['description'])

    def test_the_office_figures_are_kept(self):
        res = self._make(amounts=['160', '100'])
        self.assertEqual(res.status_code, 201, res.content)
        shares = list(PaymentSplitShare.objects.order_by('position'))
        self.assertEqual([s.monthly_amount for s in shares], [Decimal('160.00'), Decimal('100.00')])
        self.assertEqual(sum(s.registration_fee for s in shares), Decimal('120.00'))

    def test_a_payer_needs_a_phone(self):
        res = self._make(payers=[{'parent_id': str(self.mother.id)}, {'name': 'אבי'}])
        self.assertEqual(res.status_code, 400)
        self.assertFalse(PaymentSplit.objects.exists())

    def test_a_parent_of_another_family_is_refused(self):
        other = Parent.objects.create(family=Family.objects.create(name='אחר', phone='0509'), first_name='x', last_name='y', phone='0509')
        res = self._make(payers=[{'parent_id': str(self.mother.id)}, {'parent_id': str(other.id)}])
        self.assertEqual(res.status_code, 400)

    def test_a_child_who_already_pays_is_split_from_the_standing_order_instead(self):
        self._pay_whole()
        res = self._make()
        self.assertEqual(res.status_code, 400)
        self.assertIn('כבר יש הוראת קבע', res.data['error'])

    def _pay_whole(self):
        link = CardLink.objects.create(kind='standing_order', child=self.child, lesson=self.lesson, branch=self.branch)
        self._pay(link)
        return RecurringPayment.objects.get(child=self.child)

    def test_the_link_is_sent_to_the_payer_and_not_to_the_primary_parent(self):
        self._make()
        second = self._links()[1]
        with patch('apps.customers.card_link.ManyChatService') as manychat:
            manychat.return_value.notify_registration.return_value = {'sent': True}
            send_card_link_whatsapp(second, 'https://kogo.test')
        kwargs = manychat.return_value.notify_registration.call_args.kwargs
        self.assertEqual(kwargs['phone'], '0502222222')
        self.assertEqual(kwargs['parent_name'], 'אבי כהן')

    def test_the_parents_page_says_it_is_a_part(self):
        self._make()
        payload = preview_payload(self._links()[1])
        self.assertEqual(payload['split']['position'], 2)
        self.assertEqual(payload['split']['parts'], 2)
        self.assertEqual(payload['split']['monthly_total'], '260.00')
        self.assertEqual(payload['monthly_amount'], '130.00')

    def test_a_split_nobody_paid_into_can_be_cancelled(self):
        self._make()
        split = PaymentSplit.objects.get()
        res = self.client.post(f'{SPLITS}{split.id}/cancel/', {}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(set(CardLink.objects.values_list('status', flat=True)), {CardLink.STATUS_CANCELLED})
        with self.assertRaises(CardLinkError):
            apply_card_link(self._links()[0], CARD)


class PayingTests(_Base):
    def setUp(self):
        super().setUp()
        self._make()
        self.first, self.second = self._links()

    def test_the_first_payer_opens_their_standing_order_and_the_child_starts(self):
        self._pay(self.first, 'tok_mother')
        order = RecurringPayment.objects.get(child=self.child)
        self.assertEqual(order.amount, Decimal('130.00'))
        self.assertEqual(order.split_share.position, 1)
        self.assertEqual(order.tranzila_token, 'tok_mother')
        payment = order.initial_payment
        self.assertEqual(payment.parent, self.mother)
        self.assertEqual(payment.split_share_id, order.split_share_id)
        self.assertTrue(LessonEnrollment.objects.filter(child=self.child, lesson=self.lesson, status='active').exists())
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, 'active')

    def test_until_the_second_pays_the_card_says_so(self):
        self._pay(self.first)
        self.assertIn(SPLIT_INCOMPLETE, _codes(self.child))

    def test_both_paid_is_one_bill_not_a_double(self):
        self._pay(self.first, 'tok_mother')
        self._pay(self.second, 'tok_father')
        orders = list(RecurringPayment.objects.filter(child=self.child).order_by('split_share__position'))
        self.assertEqual([o.amount for o in orders], [Decimal('130.00'), Decimal('130.00')])
        self.assertEqual([o.tranzila_token for o in orders], ['tok_mother', 'tok_father'])
        codes = _codes(self.child)
        self.assertNotIn(DOUBLE_STANDING_ORDER, codes)
        self.assertNotIn(DOUBLE_CHARGE, codes)
        self.assertNotIn(SPLIT_INCOMPLETE, codes)

    def test_each_first_receipt_is_in_the_payers_name(self):
        self._pay(self.first)
        self._pay(self.second)
        invoices = list(Invoice.objects.order_by('created_at'))
        self.assertEqual([i.payer_name for i in invoices], ['דנה לוי', 'אבי כהן'])
        self.assertEqual(invoice_recipients(invoices[1]), ['avi@test.com'])

    def test_the_family_is_told_of_the_registration_once(self):
        with patch('apps.core.payment_service.PaymentService._send_registration_whatsapp') as welcome:
            self._pay(self.first)
            self._pay(self.second)
        self.assertEqual(welcome.call_count, 1)

    def test_one_payer_cannot_open_a_second_order(self):
        self._pay(self.first)
        again = CardLink.objects.create(
            kind='standing_order', child=self.child, lesson=self.lesson, branch=self.branch,
            split_share=self.first.split_share,
        )
        with self.assertRaises(CardLinkError):
            apply_card_link(again, CARD)

    def test_a_plain_link_for_the_same_class_is_still_refused(self):
        self._pay(self.first)
        plain = CardLink.objects.create(kind='standing_order', child=self.child, lesson=self.lesson, branch=self.branch)
        with self.assertRaises(CardLinkError):
            apply_card_link(plain, CARD)


class MonthlyTests(_Base):
    def setUp(self):
        super().setUp()
        self._make()
        first, second = self._links()
        self._pay(first, 'tok_mother')
        self._pay(second, 'tok_father')
        today = timezone.localdate()
        # The sign-up was last month; today is the first monthly charge.
        firsts = list(RecurringPayment.objects.filter(child=self.child).values_list('initial_payment_id', flat=True))
        before = timezone.now() - timedelta(days=35)
        Payment.objects.filter(id__in=firsts).update(payment_date=before, created_at=before)
        RecurringPayment.objects.filter(child=self.child).update(
            next_billing_date=today, last_charge_date=None, start_date=before.date(),
        )

    @patch('apps.customers.recurring_billing.TranzilaService')
    def test_each_payer_is_charged_their_part_on_their_card_and_receipted_by_name(self, tranzila_cls):
        tranzila_cls.for_saved_card.return_value.charge_with_token.side_effect = [
            {'success': True, 'transaction_id': f'T{n}', 'confirmation_code': 'C', 'response_code': '000', 'raw_response': {}}
            for n in range(2)
        ]
        summary = process_due_recurring_charges()
        self.assertEqual(summary['charged'], 2)
        calls = tranzila_cls.for_saved_card.return_value.charge_with_token.call_args_list
        self.assertEqual(sorted((c.kwargs['token'], c.kwargs['amount']) for c in calls),
                         [('tok_father', Decimal('130.00')), ('tok_mother', Decimal('130.00'))])
        firsts = RecurringPayment.objects.filter(child=self.child).values_list('initial_payment_id', flat=True)
        monthly = Payment.objects.filter(child=self.child, status='completed').exclude(id__in=list(firsts))
        self.assertEqual(monthly.count(), 2)
        self.assertEqual({p.parent_id for p in monthly}, {self.mother.id, None})
        self.assertTrue(all('מתוך 2' in p.description for p in monthly))
        names = set(Invoice.objects.filter(payment__in=monthly).values_list('payer_name', flat=True))
        self.assertEqual(names, {'דנה לוי', 'אבי כהן'})
        codes = _codes(self.child)
        self.assertNotIn(DOUBLE_CHARGE, codes)
        from apps.core.daily_brief import check_duplicate_charges
        self.assertEqual(check_duplicate_charges(timezone.localdate()).count, 0)

    @patch('apps.customers.recurring_billing.TranzilaService')
    def test_one_declined_card_does_not_stop_the_other(self, tranzila_cls):
        def charge(**kwargs):
            if kwargs['token'] == 'tok_father':
                return {'success': False, 'error': 'סירוב', 'response_code': '004', 'raw_response': {}}
            return {'success': True, 'transaction_id': 'T1', 'confirmation_code': 'C', 'response_code': '000', 'raw_response': {}}
        tranzila_cls.for_saved_card.return_value.charge_with_token.side_effect = charge
        with patch('apps.customers.card_update.ManyChatService') as manychat:
            manychat.return_value.notify_registration.return_value = {'sent': True}
            process_due_recurring_charges()
        orders = {o.tranzila_token: o.status for o in RecurringPayment.objects.filter(child=self.child)}
        self.assertEqual(orders['tok_mother'], 'active')
        self.assertNotEqual(orders['tok_father'], 'active')
        for call in manychat.return_value.notify_registration.call_args_list:
            self.assertEqual(call.kwargs['phone'], '0502222222')


class SplittingAnExistingOrderTests(_Base):
    def setUp(self):
        super().setUp()
        link = CardLink.objects.create(kind='standing_order', child=self.child, lesson=self.lesson, branch=self.branch)
        self._pay(link, 'tok_mother')
        self.order = RecurringPayment.objects.get(child=self.child)
        self.child.refresh_from_db()
        self.paid_until = self.child.paid_until_date

    def _split(self, **extra):
        body = {'recurring_payment_id': str(self.order.id), 'payers': [{'name': 'אבי כהן', 'phone': '0502222222'}]}
        body.update(extra)
        return self.client.post(f'{SPLITS}from-standing-order/', body, format='json')

    def test_the_card_on_it_stays_the_first_payer_and_pays_the_whole_until_another_card_is_saved(self):
        res = self._split()
        self.assertEqual(res.status_code, 201, res.content)
        self.order.refresh_from_db()
        self.assertEqual(self.order.split_share.position, 1)
        self.assertEqual(self.order.amount, Decimal('260.00'))
        self.assertIsNone(self.order.pending_amount)
        link = CardLink.objects.get(split_share__position=2)
        quote = quote_standing_order(link)
        self.assertEqual(quote['first_charge'], Decimal('0.00'))
        self.assertEqual(quote['monthly_amount'], Decimal('130.00'))
        self.assertEqual(quote['next_billing_date'], self.order.next_billing_date)
        # The card on it still pays the whole: nothing is short, so no mark.
        self.assertNotIn(SPLIT_INCOMPLETE, _codes(self.child))

    def test_when_the_new_payer_saves_a_card_the_first_steps_down_from_the_next_charge(self):
        self._split()
        link = CardLink.objects.get(split_share__position=2)
        self._pay(link, 'tok_father')
        self.order.refresh_from_db()
        self.assertEqual(self.order.amount, Decimal('260.00'))
        self.assertEqual(self.order.pending_amount, Decimal('130.00'))
        new = RecurringPayment.objects.get(tranzila_token='tok_father')
        self.assertEqual(new.amount, Decimal('130.00'))
        self.assertEqual(new.next_billing_date, self.order.next_billing_date)
        self.child.refresh_from_db()
        self.assertEqual(self.child.paid_until_date, self.paid_until)
        self.assertFalse(Payment.objects.filter(split_share=new.split_share, final_amount__gt=0, status='completed').exists())
        self.assertNotIn(DOUBLE_STANDING_ORDER, _codes(self.child))

    def test_an_order_already_split_is_refused(self):
        self._split()
        res = self._split()
        self.assertEqual(res.status_code, 400)


class MovingASplitChildTests(_Base):
    def setUp(self):
        super().setUp()
        self._make()
        first, second = self._links()
        self._pay(first, 'tok_mother')
        self._pay(second, 'tok_father')
        self.enrollment = LessonEnrollment.objects.get(child=self.child, lesson=self.lesson)

    def _move_to(self, lesson):
        return self.client.post(
            f'/api/v1/enrollments/lesson-enrollments/{self.enrollment.id}/change-lesson/',
            {'lesson_id': str(lesson.id)}, format='json',
        )

    def test_a_move_at_the_same_price_keeps_every_payer(self):
        other = Lesson.objects.create(course=self.course, room=self.lesson.room, day_of_week=2, start_time=time(17, 0), end_time=time(18, 0))
        res = self._move_to(other)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(
            sorted(RecurringPayment.objects.filter(child=self.child).values_list('amount', 'pending_amount')),
            [(Decimal('130.00'), None), (Decimal('130.00'), None)],
        )

    def test_a_move_that_changes_the_price_is_refused(self):
        dear = Course.objects.create(course_type=self.course.course_type, name='יקר', price=Decimal('300.00'), capacity=12, branch=self.branch)
        other = Lesson.objects.create(course=dear, room=self.lesson.room, day_of_week=2, start_time=time(17, 0), end_time=time(18, 0))
        res = self._move_to(other)
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn('מחולק', res.data['error'])
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.lesson_id, self.lesson.id)
