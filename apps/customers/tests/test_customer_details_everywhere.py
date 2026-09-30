"""
An edit on the child's card reaches every place that reads the customer.

The office changes the parent's phone, email, name and ID, the family's name
and address, and adds an extra phone — through PATCH children/{id}/details/.
Each test then asks one reader, through its own code, what it now sees: the
list, search, the WhatsApp context, reminders, office alerts, the ManyChat
contact index, the card-replacement list, the widget's returning-parent lookup,
walk-in matching and the next receipt. A receipt issued before the edit keeps
what it was issued with.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.enrollment_whatsapp import build_enrollment_whatsapp_context
from apps.core.manychat_contact_index import SCOPE_ALL, contact_index_rows, family_whatsapp_phone
from apps.core.models import UserProfile
from apps.core.office_alerts import describe_family
from apps.core.payment_service import PaymentService
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.broadcast import broadcast_to_children
from apps.customers.card_replacement import children_without_standing_order
from apps.customers.models import Child, Family, Parent, Payment
from apps.customers.views import _find_child_for_walk_in
from apps.enrollments.models import LessonEnrollment
from apps.enrollments.person_match import contact_phone
from apps.enrollments.trial_reminders import _parent_contact_for_child

User = get_user_model()

OLD_PHONE, NEW_PHONE = '050-777-8899', '0549998877'
OLD_EMAIL, NEW_EMAIL = 'old@example.com', 'new@example.com'
OLD_ID, NEW_ID = '000000018', '123456782'
EXTRA_PHONE = '0525556666'


class EditReachesEveryReaderTests(APITestCase):
    def setUp(self):
        user = User.objects.create_user(username='m-everywhere@test', password='pw-for-tests')
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save(update_fields=['role'])
        self.client.force_authenticate(User.objects.get(pk=user.pk))

        self.lesson = TestDataFactory.create_lesson()
        branch = self.lesson.course.branch
        self.family = Family.objects.create(
            name='כהן', phone=OLD_PHONE, email=OLD_EMAIL, parent_id_number=OLD_ID,
            address='הרצל 1', branch=branch,
        )
        self.parent = Parent.objects.create(
            family=self.family, first_name='יעל', last_name='כהן',
            phone=OLD_PHONE, email=OLD_EMAIL, is_primary=True,
        )
        self.child = Child.objects.create(
            family=self.family, first_name='נועה', last_name='כהן',
            birth_date=date(2016, 5, 5), gender='female', status='active',
        )
        LessonEnrollment.objects.create(
            child=self.child, lesson=self.lesson, status='active', start_date=date(2026, 9, 1),
        )
        # Issued before the edit: it must keep what it was issued with.
        self.receipt_before = PaymentService()._create_invoice_from_payment(
            self._payment(), None, send_email=False,
        )

        res = self.client.patch(
            f'/api/v1/customers/children/{self.child.id}/details/',
            {
                'parent': {'first_name': 'יעלי', 'phone': '054-999-8877', 'email': NEW_EMAIL, 'id_number': NEW_ID},
                'family': {'name': 'כהן-לוי', 'address': 'הרצל 2'},
                'extra_phones': [{'name': 'סבתא רחל', 'phone': EXTRA_PHONE}],
            },
            format='json',
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.row = res.data['child']
        self.family.refresh_from_db()
        self.parent.refresh_from_db()
        self.child = Child.objects.select_related('family').get(pk=self.child.pk)

    def _payment(self):
        return Payment.objects.create(
            child=self.child, family=self.family, parent=self.parent, lesson=self.lesson,
            branch=self.lesson.course.branch, payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('250.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('250.00'),
            payment_date=timezone.now() - timedelta(days=1),
        )

    # -- the records themselves --------------------------------------------------

    def test_both_copies_of_phone_and_email_hold_the_new_values(self):
        self.assertEqual((self.parent.phone, self.family.phone), (NEW_PHONE, NEW_PHONE))
        self.assertEqual((self.parent.email, self.family.email), (NEW_EMAIL, NEW_EMAIL))
        self.assertEqual(self.parent.first_name, 'יעלי')
        self.assertTrue(self.parent.is_primary)
        self.assertEqual(self.family.parent_id_number, NEW_ID)
        self.assertEqual((self.family.name, self.family.address), ('כהן-לוי', 'הרצל 2'))
        extra = Parent.objects.get(family=self.family, is_primary=False)
        self.assertEqual(extra.phone, EXTRA_PHONE)
        # Nothing anywhere still holds the old number.
        self.assertFalse(Parent.objects.filter(phone=OLD_PHONE).exists())
        self.assertFalse(Family.objects.filter(phone=OLD_PHONE).exists())

    def test_nothing_about_money_or_status_moved(self):
        self.assertEqual(self.child.status, 'active')
        self.assertEqual(Payment.objects.filter(family=self.family).count(), 1)
        self.assertEqual(self.family.branch_id, self.lesson.course.branch_id)

    # -- the CRM -----------------------------------------------------------------

    def test_the_list_row_the_card_and_table_read(self):
        row = self.client.get('/api/v1/customers/children/', {'family': str(self.family.id)}).data['results'][0]
        self.assertEqual(row, self.row)
        self.assertEqual(row['parent_phone'], NEW_PHONE)
        self.assertEqual(row['family_phone'], NEW_PHONE)
        self.assertEqual(row['parent_email'], NEW_EMAIL)
        self.assertEqual(row['family_email'], NEW_EMAIL)
        self.assertEqual(row['parent_name'], 'יעלי כהן')
        self.assertEqual(row['parent_id_number'], NEW_ID)
        self.assertEqual(row['family_name'], 'כהן-לוי')
        self.assertEqual([e['phone'] for e in row['extra_phones']], [EXTRA_PHONE])

    def test_search_finds_the_new_details_and_not_the_old(self):
        def found(term):
            rows = self.client.get('/api/v1/customers/children/', {'search': term}).data['results']
            return {r['id'] for r in rows}
        me = {str(self.child.id)}
        for term in ('0549998877', '054-999-8877', '972549998877', NEW_ID, EXTRA_PHONE, 'יעלי'):
            self.assertEqual(found(term), me, term)
        self.assertEqual(found('0507778899'), set())

    def test_the_children_without_a_standing_order_list(self):
        rows = [r for r in children_without_standing_order() if r['child_id'] == str(self.child.id)]
        self.assertEqual([r['parent_phone'] for r in rows], [NEW_PHONE])
        self.assertEqual(rows[0]['family_name'], 'כהן-לוי')

    # -- messages ----------------------------------------------------------------

    def test_whatsapp_goes_to_the_new_phone_by_the_new_name(self):
        ctx = build_enrollment_whatsapp_context(child=self.child, lesson=self.lesson)
        self.assertEqual(ctx['phone'], NEW_PHONE)
        self.assertEqual(ctx['parent_name'], 'יעלי כהן')

    def test_trial_reminders(self):
        name, phone, email = _parent_contact_for_child(self.child)
        self.assertEqual((name, phone, email), ('יעלי כהן', NEW_PHONE, NEW_EMAIL))

    def test_office_alerts(self):
        line = describe_family(self.family)
        self.assertIn('יעלי כהן', line)
        self.assertIn(NEW_PHONE, line)

    def test_the_manychat_contact_index(self):
        parents = list(self.family.parents.all())
        self.assertEqual(family_whatsapp_phone(self.family, parents), NEW_PHONE)
        phones = [row[0] for row in contact_index_rows(SCOPE_ALL)]
        self.assertIn('972549998877', phones)
        self.assertNotIn('972507778899', phones)

    def test_a_group_message_reaches_the_new_phone_and_the_extra_one(self):
        out = broadcast_to_children([self.child], automation_type='flow', automation_id='x',
                                    include_extra_phones=True)
        row = out['results'][0]
        self.assertEqual((row['phone'], row['status']), ('972549998877', 'preview'))
        self.assertEqual([(e['phone'], e['status']) for e in row['extra_phones']], [('972525556666', 'preview')])

    # -- identity: registration and walk-ins -------------------------------------

    def test_the_widget_finds_the_returning_parent_by_the_new_id(self):
        def lookup(parent_id):
            return self.client.post('/api/v1/customers/widget/lookup/', {
                'parent_id_number': parent_id, 'child_first_name': 'נועה', 'child_last_name': 'כהן',
            }, format='json').data['family_status']
        self.assertNotEqual(lookup(NEW_ID), 'new')
        self.assertEqual(lookup(OLD_ID), 'new')

    def test_a_walk_in_is_matched_by_the_new_phone(self):
        self.assertEqual(contact_phone(self.child), NEW_PHONE)
        self.assertEqual(
            _find_child_for_walk_in(first_name='נועה', last_name='כהן', phone='054-999-8877'), self.child,
        )
        self.assertIsNone(_find_child_for_walk_in(first_name='נועה', last_name='כהן', phone=OLD_PHONE))

    # -- documents -----------------------------------------------------------------

    def test_the_next_receipt_carries_the_new_details(self):
        # payer_email is also where the receipt is mailed (subscription_invoice_email).
        receipt = PaymentService()._create_invoice_from_payment(self._payment(), None, send_email=False)
        self.assertEqual(
            (receipt.payer_name, receipt.payer_phone, receipt.payer_email),
            ('כהן-לוי', NEW_PHONE, NEW_EMAIL),
        )

    def test_a_receipt_issued_before_keeps_what_it_was_issued_with(self):
        self.receipt_before.refresh_from_db()
        self.assertEqual(
            (self.receipt_before.payer_name, self.receipt_before.payer_phone, self.receipt_before.payer_email),
            ('כהן', OLD_PHONE, OLD_EMAIL),
        )


@override_settings(
    EMAIL_HOST='smtp.test',
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    RESEND_API_KEY='',
    DOCUMENT_SIGNING_ENABLED=False,
)
class TwoParentsTests(APITestCase):
    """
    A child with two parents: the mother registered, and the office adds the
    father on the card with his phone and his email. A group message the
    office sends reaches both, and so does the monthly receipt.
    """

    def setUp(self):
        user = User.objects.create_user(username='m-two-parents@test', password='pw-for-tests')
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = UserProfile.ROLE_MANAGER
        profile.save(update_fields=['role'])
        self.client.force_authenticate(User.objects.get(pk=user.pk))
        self.lesson = TestDataFactory.create_lesson()
        self.family = Family.objects.create(
            name='כהן', phone='0507778899', email='mom@example.com', branch=self.lesson.course.branch,
        )
        Parent.objects.create(family=self.family, first_name='יעל', last_name='כהן',
                              phone='0507778899', email='mom@example.com', is_primary=True)
        self.child = Child.objects.create(family=self.family, first_name='נועה', last_name='כהן',
                                          birth_date=date(2016, 5, 5), gender='female', status='active')
        LessonEnrollment.objects.create(child=self.child, lesson=self.lesson, status='active',
                                        start_date=date(2026, 9, 1))
        res = self.client.patch(f'/api/v1/customers/children/{self.child.id}/details/', {
            'extra_phones': [{'name': 'דני כהן', 'phone': '052-111-2233', 'email': 'dad@example.com'}],
        }, format='json')
        self.assertEqual(res.status_code, 200, res.content)

    def test_a_group_message_goes_to_both_parents(self):
        from unittest.mock import patch

        with patch('apps.customers.views.ManyChatService.is_configured', new=property(lambda self: True)), \
             patch('apps.customers.broadcast.ManyChatService.notify_registration',
                   return_value={'sent': True}) as send:
            res = self.client.post('/api/v1/customers/children/broadcast/', {
                'child_ids': [str(self.child.id)], 'automation_type': 'kind', 'automation_id': 'subscription',
                'dry_run': False, 'include_extra_phones': True,
            }, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(
            [(c.kwargs['phone'], c.kwargs['parent_name'], c.kwargs['child_name']) for c in send.call_args_list],
            [('0507778899', 'יעל כהן', 'נועה כהן'), ('0521112233', 'דני כהן', 'נועה כהן')],
        )
        self.assertEqual(res.data['phones'], ['972507778899', '972521112233'])

    def test_the_monthly_receipt_goes_to_both_parents(self):
        from django.core import mail
        from apps.customers.subscription_invoice_email import send_subscription_invoice_email

        payment = Payment.objects.create(
            child=self.child, family=self.family, lesson=self.lesson, branch=self.lesson.course.branch,
            payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('250.00'), discount_amount=Decimal('0.00'), final_amount=Decimal('250.00'),
            payment_date=timezone.now(),
        )
        receipt = PaymentService()._create_invoice_from_payment(payment, None, send_email=False)
        mail.outbox = []
        self.assertTrue(send_subscription_invoice_email(receipt))
        self.assertEqual([m.to for m in mail.outbox], [['mom@example.com', 'dad@example.com']])

    def test_payment_and_card_links_still_go_to_the_registered_parent_only(self):
        ctx = build_enrollment_whatsapp_context(child=self.child, lesson=self.lesson)
        self.assertEqual(ctx['phone'], '0507778899')
