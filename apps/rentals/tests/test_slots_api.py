"""Linking slots to tenancies, the suggestions that group unlinked rentals, and the all-or-nothing import."""
from datetime import date
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.customers.models import BusinessCustomer
from apps.rentals.models import Tenancy
from apps.rentals.tests.factories import make_branch, make_customer, make_rental, make_user
from apps.scheduling.models import ScheduleEvent

URL = '/api/v1/rentals/tenancies/'


def make_meeting(branch):
    """A calendar event that is not a studio rental."""
    return ScheduleEvent.objects.create(name='ישיבת צוות', event_date=date(2026, 9, 6), branch=branch)


class SlotTestCase(APITestCase):
    def setUp(self):
        self.manager = make_user('manager-slots@test', UserProfile.ROLE_MANAGER)
        self.client.force_authenticate(self.manager)
        self.branch = make_branch('פלורנטין')
        self.other_branch = make_branch('רמת אביב')

    def tenancy(self, branch=None, tenant=None):
        return Tenancy.objects.create(
            tenant=tenant or make_customer(), branch=branch or self.branch, monthly_amount=Decimal('1'),
        )

    def link(self, tenancy, *slots):
        return self.client.post(
            f'{URL}{tenancy.id}/link-slots/', {'slot_ids': [str(slot.id) for slot in slots]}, format='json',
        )

    def unlink(self, tenancy, payload):
        return self.client.post(f'{URL}{tenancy.id}/unlink-slot/', payload, format='json')

    def suggestions(self):
        res = self.client.get(f'{URL}suggestions/')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        return res.data


class LinkSlotsTests(SlotTestCase):
    def test_links_studio_rentals_of_the_tenancys_branch(self):
        tenancy = self.tenancy()
        first = make_rental(self.branch, price='100', days=(0,))
        second = make_rental(self.branch, price='150', days=(1, 3))
        res = self.link(tenancy, first, second)
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual({slot['id'] for slot in res.data['slots']}, {str(first.id), str(second.id)})
        # 100 × 4 + 150 × 4 × 2
        self.assertEqual(res.data['suggested_monthly_amount'], '1600.00')
        self.assertEqual(set(tenancy.slots.values_list('id', flat=True)), {first.id, second.id})

    def test_linking_a_slot_again_is_harmless(self):
        tenancy = self.tenancy()
        slot = make_rental(self.branch)
        self.link(tenancy, slot)
        res = self.link(tenancy, slot)
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(len(res.data['slots']), 1)

    def test_refuses_an_event_that_is_not_a_studio_rental(self):
        meeting = make_meeting(self.branch)
        res = self.link(self.tenancy(), meeting)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data['error'], '"ישיבת צוות" אינו שכירות סטודיו')
        self.assertEqual(res.data['slot_id'], str(meeting.id))

    def test_refuses_a_rental_in_another_branch(self):
        elsewhere = make_rental(self.other_branch, renter_name='יוגה בר')
        res = self.link(self.tenancy(), elsewhere)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data['error'], 'השכירות של "יוגה בר" בסניף אחר מסניף ההסכם')

    def test_refuses_a_rental_another_tenancy_holds(self):
        holder = self.tenancy(tenant=make_customer('רון', 'כהן'))
        slot = make_rental(self.branch, renter_name='רון כהן', tenancy=holder)
        res = self.link(self.tenancy(), slot)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data['error'], 'השכירות של "רון כהן" כבר משויכת להסכם שכירות אחר (רון כהן)')
        slot.refresh_from_db()
        self.assertEqual(slot.tenancy_id, holder.id)

    def test_all_of_them_or_none(self):
        tenancy = self.tenancy()
        good = make_rental(self.branch)
        bad = make_rental(self.other_branch)
        self.assertEqual(self.link(tenancy, good, bad).status_code, status.HTTP_400_BAD_REQUEST)
        good.refresh_from_db()
        self.assertIsNone(good.tenancy_id)

    def test_unknown_malformed_and_empty_ids(self):
        tenancy = self.tenancy()
        post = lambda body: self.client.post(f'{URL}{tenancy.id}/link-slots/', body, format='json')  # noqa: E731
        cases = (
            ({'slot_ids': ['00000000-0000-0000-0000-000000000000']}, 'השכירות לא נמצאה'),
            ({'slot_ids': ['nope']}, 'השכירות לא נמצאה'),
            ({'slot_ids': []}, 'יש לבחור לפחות שכירות אחת'),
            ({'slot_ids': 'x'}, 'slot_ids חייב להיות רשימה'),
            ({}, 'slot_ids חייב להיות רשימה'),
        )
        for body, message in cases:
            with self.subTest(body=body):
                res = post(body)
                self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(res.data['error'], message)

    def test_a_tenancy_without_a_branch_takes_no_slots(self):
        tenancy = Tenancy.objects.create(tenant=make_customer(), monthly_amount=Decimal('1'))
        res = self.link(tenancy, make_rental(self.branch))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data['error'], 'יש לבחור סניף להסכם לפני שיוך שכירויות')


class UnlinkSlotTests(SlotTestCase):
    def test_lets_one_slot_go_and_keeps_the_event(self):
        tenancy = self.tenancy()
        keep = make_rental(self.branch, tenancy=tenancy)
        drop = make_rental(self.branch, tenancy=tenancy)
        res = self.unlink(tenancy, {'slot_id': str(drop.id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual([slot['id'] for slot in res.data['slots']], [str(keep.id)])
        drop.refresh_from_db()
        self.assertIsNone(drop.tenancy_id)

    def test_refuses_a_slot_this_tenancy_does_not_hold(self):
        tenancy = self.tenancy()
        other = self.tenancy()
        slot = make_rental(self.branch, tenancy=other)
        res = self.unlink(tenancy, {'slot_id': str(slot.id)})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data['error'], 'השכירות אינה משויכת להסכם הזה')
        slot.refresh_from_db()
        self.assertEqual(slot.tenancy_id, other.id)
        self.assertEqual(self.unlink(tenancy, {}).data['error'], 'יש לבחור שכירות לניתוק')
        self.assertEqual(self.unlink(tenancy, {'slot_id': 'nope'}).data['error'], 'השכירות לא נמצאה')


class SuggestionsTests(SlotTestCase):
    def test_groups_by_id_digits_and_splits_by_branch(self):
        first = make_rental(
            self.branch, renter_name='סטודיו אור', renter_id_number='51-234567-8', price='100', days=(0,),
            contract=(date(2026, 9, 1), date(2027, 6, 30)),
        )
        second = make_rental(
            self.branch, renter_name='סטודיו אור', renter_id_number='512345678', price='150', days=(2, 4),
            contract=(date(2026, 10, 1), date(2027, 8, 31)),
        )
        elsewhere = make_rental(self.other_branch, renter_name='סטודיו אור', renter_id_number='512345678')
        groups = self.suggestions()
        self.assertEqual(len(groups), 2)
        by_key = {group['key']: group for group in groups}
        here = by_key[f'id:512345678:{self.branch.id}']
        there = by_key[f'id:512345678:{self.other_branch.id}']
        self.assertEqual({slot['id'] for slot in here['slots']}, {str(first.id), str(second.id)})
        self.assertEqual([slot['id'] for slot in there['slots']], [str(elsewhere.id)])
        self.assertEqual(here['renter_name'], 'סטודיו אור')
        self.assertEqual(here['renter_id_number'], '512345678')
        self.assertEqual(here['branch'], str(self.branch.id))
        self.assertEqual(here['branch_name'], 'פלורנטין')
        # 100 × 4 + 150 × 4 × 2
        self.assertEqual(here['suggested_monthly_amount'], '1600.00')
        self.assertEqual(here['contract_start_date'], '2026-09-01')
        self.assertEqual(here['contract_end_date'], '2027-08-31')
        self.assertIsNone(here['existing_tenant'])

    def test_rentals_without_an_id_are_never_merged_by_name(self):
        first = make_rental(self.branch, renter_name='דני', renter_id_number='')
        second = make_rental(self.branch, renter_name='דני', renter_id_number='—')
        groups = self.suggestions()
        self.assertEqual({group['key'] for group in groups}, {f'event:{first.id}', f'event:{second.id}'})
        self.assertTrue(all(len(group['slots']) == 1 for group in groups))
        self.assertTrue(all(group['existing_tenant'] is None for group in groups))

    def test_names_the_merchant_already_on_file(self):
        company = make_customer('סטודיו', 'אור', company_number='51-234567-8')
        person = make_customer('יעל', 'בר', id_number='012345678')
        make_rental(self.branch, renter_id_number='512345678')
        make_rental(self.branch, renter_id_number='0123-4567-8')
        groups = {group['key']: group for group in self.suggestions()}
        self.assertEqual(groups[f'id:512345678:{self.branch.id}']['existing_tenant'], {
            'id': str(company.id), 'full_name': 'סטודיו אור', 'company_number': '51-234567-8', 'id_number': '',
        })
        self.assertEqual(groups[f'id:012345678:{self.branch.id}']['existing_tenant']['id'], str(person.id))

    def test_renter_name_is_the_spelling_used_most(self):
        make_rental(self.branch, renter_name='סטודיו אור', renter_id_number='512345678')
        make_rental(self.branch, renter_name='אור סטודיו', renter_id_number='512345678')
        make_rental(self.branch, renter_name='אור סטודיו', renter_id_number='512345678')
        (group,) = self.suggestions()
        self.assertEqual(group['renter_name'], 'אור סטודיו')

    def test_leaves_out_what_cannot_be_linked(self):
        make_rental(self.branch, renter_id_number='111', tenancy=self.tenancy())
        make_rental(self.branch, renter_id_number='222', is_active=False)
        make_rental(None, renter_id_number='333')
        make_meeting(self.branch)
        self.assertEqual(self.suggestions(), [])


class ImportTests(SlotTestCase):
    def post(self, groups):
        return self.client.post(f'{URL}import/', {'groups': groups}, format='json')

    def test_one_tenancy_per_group_with_its_slots(self):
        first = make_rental(self.branch, renter_id_number='512345678', price='100')
        second = make_rental(self.branch, renter_id_number='512345678', price='150', days=(1, 2))
        elsewhere = make_rental(self.other_branch, renter_id_number='300000001', price='90')
        existing = make_customer('יוגה', 'בר', id_number='300000001')
        res = self.post([
            {
                'slot_ids': [str(first.id), str(second.id)],
                'tenant': {'first_name': 'סטודיו אור', 'company_number': '512345678'},
                'monthly_amount': '1600.00', 'billing_day': 5,
                'start_date': '2026-09-01', 'end_date': '2027-08-31', 'status': 'active',
            },
            {'slot_ids': [str(elsewhere.id)], 'tenant_id': str(existing.id), 'monthly_amount': '360.00'},
        ])
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        created, other = res.data
        self.assertEqual(str(created['branch']), str(self.branch.id))
        self.assertEqual(created['status'], 'active')
        self.assertEqual(created['billing_day'], 5)
        self.assertEqual({slot['id'] for slot in created['slots']}, {str(first.id), str(second.id)})
        self.assertEqual(BusinessCustomer.objects.get(pk=created['tenant']['id']).business.name, 'סוחרים')
        self.assertEqual(BusinessCustomer.objects.get(pk=created['tenant']['id']).branch_id, self.branch.id)
        self.assertEqual(str(other['branch']), str(self.other_branch.id))
        self.assertEqual(other['tenant']['id'], str(existing.id))
        self.assertEqual(other['status'], 'draft')
        self.assertEqual(self.suggestions(), [])

    def test_one_bad_group_rolls_back_the_whole_import(self):
        first = make_rental(self.branch, renter_id_number='512345678')
        second = make_rental(self.branch, renter_id_number='300000001')
        res = self.post([
            {'slot_ids': [str(first.id)], 'tenant': {'first_name': 'סטודיו אור'}, 'monthly_amount': '400'},
            {'slot_ids': [str(second.id)], 'tenant': {'first_name': 'יוגה'}, 'monthly_amount': '-5'},
        ])
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data['group'], 1)
        self.assertTrue(res.data['error'].startswith('קבוצה 2: '), res.data['error'])
        self.assertIn('monthly_amount', res.data['details'])
        self.assertFalse(Tenancy.objects.exists())
        self.assertFalse(BusinessCustomer.objects.exists())
        first.refresh_from_db()
        self.assertIsNone(first.tenancy_id)

    def test_a_slot_in_two_groups_fails_the_second(self):
        slot = make_rental(self.branch, renter_id_number='512345678')
        res = self.post([
            {'slot_ids': [str(slot.id)], 'tenant': {'first_name': 'א'}, 'monthly_amount': '1'},
            {'slot_ids': [str(slot.id)], 'tenant': {'first_name': 'ב'}, 'monthly_amount': '1'},
        ])
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data['group'], 1)
        self.assertEqual(res.data['slot_id'], str(slot.id))
        self.assertFalse(Tenancy.objects.exists())

    def test_the_slot_rules_of_linking_apply(self):
        held = make_rental(self.branch, tenancy=self.tenancy())
        here = make_rental(self.branch)
        there = make_rental(self.other_branch)
        meeting = make_meeting(self.branch)
        for slots, message in (
            ([held], 'כבר משויכת להסכם שכירות אחר'),
            ([here, there], 'כל השכירויות בקבוצה חייבות להיות באותו סניף'),
            ([meeting], 'אינו שכירות סטודיו'),
        ):
            with self.subTest(message=message):
                res = self.post([{
                    'slot_ids': [str(slot.id) for slot in slots],
                    'tenant': {'first_name': 'א'}, 'monthly_amount': '1',
                }])
                self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(res.data['group'], 0)
                self.assertIn(message, res.data['error'])
        self.assertEqual(Tenancy.objects.count(), 1)

    def test_an_empty_or_malformed_import_is_refused(self):
        self.assertEqual(self.post([]).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.post(f'{URL}import/', {}, format='json').status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.post(['not a group']).data['group'], 0)
        res = self.post([{'tenant': {'first_name': 'א'}, 'monthly_amount': '1'}])
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data['error'], 'קבוצה 1: slot_ids חייב להיות רשימה')
