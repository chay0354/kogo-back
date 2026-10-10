"""
The WhatsApp block of a customer's card (docs/WAHUB-CONTRACT-STAGE3.md, ב): the
family's phones, the contacts behind them, what is linked, and the recheck.
"""
import uuid

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Child, Family, Parent
from apps.wahub.models import Contact
from apps.wahub.tests.base import WahubTestCase

PARENT = '972505550101'
CHILD = '972525550303'
CONTACT_KEYS = {
    'id', 'name', 'phone', 'phone_display', 'is_demo', 'last_message_at', 'last_message_text', 'last_message_direction',
    'last_message_sender', 'handled_by', 'needs_human', 'known_summary', 'known_interest_label', 'followup_status',
    'followup_status_label', 'followup_due', 'kogo_outcome', 'kogo_outcome_label', 'linked', 'link',
}


class ForCustomerTestCase(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.family = TestDataFactory.create_family(name='לוי', phone='050-5550101')
        self.parent = TestDataFactory.create_parent(family=self.family, phone='050-555-0101', first_name='רותם', last_name='לוי')
        self.child = TestDataFactory.create_child(
            family=self.family, first_name='נועה', last_name='לוי', phone_number='052-555 0303', status='pending',
        )

    def block(self, family_id=None):
        response = self.get('for-customer/', family=str(family_id or self.family.id))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def recheck(self, family_id=None):
        response = self.post('for-customer/recheck/', {'family': str(family_id or self.family.id)})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data


class FindingTheContactsTests(ForCustomerTestCase):
    def test_the_familys_phones_in_the_spelling_a_contact_is_stored_in(self):
        data = self.block()
        self.assertEqual(data['family'], str(self.family.id))
        self.assertEqual(data['phones'], [PARENT, CHILD])     # the card's phone and the parent's are one number
        self.assertEqual(data['contacts'], [])
        self.assertIsNone(data['checked_at'])

    def test_found_by_a_parents_phone(self):
        self.incoming('אשמח לפרטים', phone=PARENT)
        contacts = self.block()['contacts']
        self.assertEqual([row['phone'] for row in contacts], [PARENT])
        self.assertEqual(contacts[0]['id'], self.contact(PARENT).id)

    def test_found_by_the_childs_own_phone(self):
        self.incoming('מתי השיעור?', phone=CHILD)
        self.assertEqual([row['phone'] for row in self.block()['contacts']], [CHILD])

    def test_found_by_an_extra_contact_of_the_family(self):
        Parent.objects.create(family=self.family, first_name='סבתא', last_name='לוי', phone='+972 54-555-0404', is_primary=False)
        self.incoming('שלום', phone='972545550404')
        data = self.block()
        self.assertEqual(data['phones'], [PARENT, '972545550404', CHILD])
        self.assertEqual([row['phone'] for row in data['contacts']], ['972545550404'])

    def test_somebody_elses_phone_is_not_on_the_card(self):
        other = TestDataFactory.create_family(name='כהן', phone='050-7000000')
        TestDataFactory.create_parent(family=other, phone='050-7000000')
        self.incoming('שלום', phone='972507000000')
        self.assertEqual(self.block()['contacts'], [])
        self.assertEqual([row['phone'] for row in self.block(other.id)['contacts']], ['972507000000'])

    def test_a_landline_and_a_walk_ins_phone_are_left_out(self):
        Family.objects.filter(pk=self.family.pk).update(phone='03-1234567')
        Parent.objects.filter(pk=self.parent.pk).update(phone='')
        TestDataFactory.create_child(family=self.family, first_name='רפאים', status='ghost', phone_number='050-9999999')
        self.assertEqual(self.block()['phones'], [CHILD])

    def test_newest_conversation_first(self):
        self.incoming('ראשון', phone=CHILD)
        self.incoming('שני', phone=PARENT)
        self.assertEqual([row['phone'] for row in self.block()['contacts']], [PARENT, CHILD])


class WhatARowSaysTests(ForCustomerTestCase):
    def test_a_row(self):
        self.incoming('אפשר לדבר עם נציג?', phone=PARENT, name='רותם')
        self.bot_reply('כבר מעבירים אותך', phone=PARENT)
        Contact.objects.filter(phone=PARENT).update(
            known_summary='מבקש נציג בעניין חיוב', known_interest='hot',
            followup_status='waiting_us', kogo_outcome='pending', kogo_family_id=self.family.id,
        )
        row = self.block()['contacts'][0]
        self.assertEqual(set(row), CONTACT_KEYS)
        contact = self.contact(PARENT)
        self.assertEqual((row['id'], row['name'], row['phone'], row['phone_display']), (contact.id, 'רותם', PARENT, '050-5550101'))
        self.assertFalse(row['is_demo'])
        self.assertEqual(row['last_message_at'], contact.last_message_at.isoformat())
        self.assertEqual(
            (row['last_message_text'], row['last_message_direction'], row['last_message_sender']),
            ('כבר מעבירים אותך', 'out', 'bot'),
        )
        self.assertEqual((row['handled_by'], row['needs_human']), ('bot', True))
        self.assertEqual((row['known_summary'], row['known_interest_label']), ('מבקש נציג בעניין חיוב', 'רוצה להירשם'))
        self.assertEqual((row['followup_status'], row['followup_status_label'], row['followup_due']), ('waiting_us', 'מחכה לנו', None))
        self.assertEqual((row['kogo_outcome'], row['kogo_outcome_label']), ('pending', 'התחיל רישום ולא סיים'))
        self.assertTrue(row['linked'])
        self.assertEqual(row['link'], f'/wahub?tab=chats&contact={contact.id}')

    def test_linked_means_the_matching_put_this_family_on_the_contact(self):
        self.incoming('שלום', phone=PARENT)
        row = self.block()['contacts'][0]
        self.assertEqual((row['kogo_outcome'], row['kogo_outcome_label'], row['linked']), ('', 'עוד לא נבדק', False))
        Contact.objects.filter(phone=PARENT).update(kogo_family_id=uuid.uuid4(), kogo_outcome='in_system')
        self.assertFalse(self.block()['contacts'][0]['linked'])
        Contact.objects.filter(phone=PARENT).update(kogo_family_id=self.family.id)
        self.assertTrue(self.block()['contacts'][0]['linked'])

    def test_a_demo_contact_is_marked(self):
        self.make_contact(phone=PARENT, name='דמו', is_demo=True, source='demo')
        self.assertTrue(self.block()['contacts'][0]['is_demo'])

    def test_checked_at_is_when_the_registrations_were_last_read(self):
        self.incoming('שלום', phone=PARENT)
        self.incoming('שלום', phone=CHILD)
        self.assertIsNone(self.block()['checked_at'])
        self.post(f'contacts/{self.contact(CHILD).id}/recheck/')
        self.assertEqual(self.block()['checked_at'], self.contact(CHILD).kogo_checked_at.isoformat())


class RefusalsTests(ForCustomerTestCase):
    def test_a_family_nobody_has_is_404_in_hebrew(self):
        response = self.get('for-customer/', family=str(uuid.uuid4()))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['detail'], 'המשפחה לא נמצאה.')
        recheck = self.post('for-customer/recheck/', {'family': str(uuid.uuid4())})
        self.assertEqual((recheck.status_code, recheck.data['detail']), (404, 'המשפחה לא נמצאה.'))

    def test_no_family_or_a_bad_id_is_400(self):
        self.assertEqual(self.get('for-customer/').status_code, 400)
        self.assertEqual(self.get('for-customer/', family='not-a-uuid').status_code, 400)
        self.assertEqual(self.post('for-customer/recheck/', {}).status_code, 400)
        self.assertEqual(self.post('for-customer/recheck/', {'family': 'x'}).status_code, 400)


class RecheckTests(ForCustomerTestCase):
    def test_recheck_links_the_contact_to_the_family_and_writes_nothing_else(self):
        self.incoming('רוצה להירשם', phone=PARENT)
        Contact.objects.filter(phone=PARENT).update(followup_status='waiting_us', followup_note='לחזור')
        before = self.contact(PARENT)
        self.assertEqual((before.kogo_outcome, before.kogo_family_id), ('', None))
        rows = {
            'family': Family.objects.values().get(pk=self.family.pk),
            'parent': Parent.objects.values().get(pk=self.parent.pk),
            'child': Child.objects.values().get(pk=self.child.pk),
        }

        data = self.recheck()
        row = data['contacts'][0]
        self.assertEqual((row['kogo_outcome'], row['kogo_outcome_label'], row['linked']), ('pending', 'התחיל רישום ולא סיים', True))
        after = self.contact(PARENT)
        self.assertEqual(after.kogo_family_id, self.family.id)
        self.assertEqual(after.kogo_child_ids, [str(self.child.id)])
        self.assertIsNotNone(after.kogo_checked_at)
        self.assertEqual(data['checked_at'], after.kogo_checked_at.isoformat())
        self.assertEqual((after.followup_status, after.followup_note), ('waiting_us', 'לחזור'))
        self.assertEqual(Family.objects.values().get(pk=self.family.pk), rows['family'])
        self.assertEqual(Parent.objects.values().get(pk=self.parent.pk), rows['parent'])
        self.assertEqual(Child.objects.values().get(pk=self.child.pk), rows['child'])

    def test_recheck_reaches_every_contact_of_the_family(self):
        self.incoming('שלום', phone=PARENT)
        self.incoming('שלום', phone=CHILD)
        data = self.recheck()
        self.assertEqual({row['phone']: row['linked'] for row in data['contacts']}, {PARENT: True, CHILD: True})
        self.assertEqual(Contact.objects.filter(kogo_family_id=self.family.id).count(), 2)

    def test_recheck_moves_a_contact_that_was_linked_elsewhere(self):
        self.incoming('שלום', phone=PARENT)
        Contact.objects.filter(phone=PARENT).update(kogo_family_id=uuid.uuid4(), kogo_outcome='not_found')
        self.assertFalse(self.block()['contacts'][0]['linked'])
        self.assertTrue(self.recheck()['contacts'][0]['linked'])
        self.assertTrue(self.block()['contacts'][0]['linked'])

    def test_recheck_with_no_contacts_is_the_empty_answer(self):
        self.assertEqual(self.recheck()['contacts'], [])
