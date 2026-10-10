"""The state of a conversation, the open conversation, and the live update."""
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from apps.wahub.models import Contact, Message
from apps.wahub.tests.base import WahubTestCase


class ConversationStateTests(WahubTestCase):
    def test_incoming_messages_count_as_unread_and_start_the_wait_once(self):
        self.incoming('ראשונה')
        first = self.contact()
        self.assertEqual(first.unread_count, 1)
        self.assertIsNotNone(first.waiting_since)
        self.assertEqual(first.first_inbound_at, first.last_inbound_at)

        self.incoming('שנייה')
        second = self.contact()
        self.assertEqual(second.unread_count, 2)
        self.assertEqual(second.messages_count, 2)
        # The wait is measured from the oldest message nobody answered.
        self.assertEqual(second.waiting_since, first.waiting_since)
        self.assertEqual(second.first_inbound_at, first.first_inbound_at)
        self.assertGreaterEqual(second.last_inbound_at, first.last_inbound_at)
        self.assertEqual(second.last_message_text, 'שנייה')
        self.assertTrue(second.needs_analysis)

    def test_a_bot_reply_ends_the_wait_and_leaves_unread_alone(self):
        self.incoming('שאלה')
        self.bot_reply('תשובה')
        contact = self.contact()
        self.assertIsNone(contact.waiting_since)
        self.assertEqual(contact.unread_count, 1)
        self.assertEqual((contact.last_message_direction, contact.last_message_sender), ('out', 'bot'))
        self.assertEqual(contact.messages_count, 2)

        self.incoming('שאלה נוספת')
        self.assertIsNotNone(self.contact().waiting_since)

    def test_reading_clears_unread_and_keeps_the_wait(self):
        self.incoming('שאלה')
        self.incoming('ועוד אחת')
        contact = self.contact()
        response = self.post(f'contacts/{contact.id}/read/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['chat']['unread_count'], 0)
        self.assertIsNotNone(response.data['chat']['waiting_since'])

    def test_opening_a_conversation_does_not_mark_it_read(self):
        self.incoming('שאלה')
        contact = self.contact()
        self.get(f'contacts/{contact.id}/')
        self.assertEqual(self.contact().unread_count, 1)

    def test_the_24_hour_window(self):
        self.incoming('שאלה')
        contact = self.contact()
        data = self.get(f'contacts/{contact.id}/').data['chat']
        self.assertTrue(data['can_free_text'])
        self.assertIsNotNone(data['window_closes_at'])

        self.age(contact, last_inbound_at=24 * 60 + 1)
        data = self.get(f'contacts/{contact.id}/').data['chat']
        self.assertFalse(data['can_free_text'])
        self.assertIsNotNone(data['window_closes_at'])

    def test_a_contact_that_never_wrote_has_no_window(self):
        contact = self.make_contact(name='ידני')
        data = self.get(f'contacts/{contact.id}/').data
        self.assertFalse(data['chat']['can_free_text'])
        self.assertIsNone(data['chat']['window_closes_at'])
        self.assertIsNone(data['last_message'])


class ConversationDetailTests(WahubTestCase):
    def test_the_contact_carries_every_group_of_the_contract(self):
        self.incoming('שלום', name='רותם')
        data = self.get(f'contacts/{self.contact().id}/').data
        self.assertEqual(data['phone'], '972505550101')
        self.assertEqual(data['phone_display'], '050-5550101')
        self.assertEqual(data['source_label'], 'וואטסאפ')
        self.assertEqual(data['last_message']['text'], 'שלום')
        self.assertEqual(
            set(data['chat']),
            {'unread_count', 'waiting_since', 'handled_by', 'handled_by_label', 'needs_human',
             'needs_human_reason', 'needs_human_at', 'can_free_text', 'window_closes_at'},
        )
        self.assertEqual(data['chat']['handled_by_label'], 'הבוט עונה')
        self.assertEqual(
            set(data['known']),
            {'topic', 'topic_label', 'course_type', 'city', 'branch_id', 'branch_name', 'child_age', 'interest',
             'interest_label', 'callback_on', 'flags', 'flag_labels', 'summary', 'analyzed_at', 'analysis_source'},
        )
        self.assertEqual(
            set(data['kogo']),
            {'outcome', 'outcome_label', 'family_id', 'child_ids', 'detail', 'checked_at', 'is_customer',
             'hidden_by_default', 'children'},
        )
        self.assertEqual(set(data['followup']), {'status', 'status_label', 'due', 'note', 'by_name', 'at', 'is_due'})
        self.assertEqual(data['tags'], [])
        self.assertEqual(data['kogo']['outcome'], '')
        self.assertEqual(data['kogo']['outcome_label'], 'עוד לא נבדק')

    def test_messages_come_oldest_first_with_the_last_fifty(self):
        self.incoming('פתיחה')
        contact = self.contact()
        Message.objects.bulk_create([
            Message(contact=contact, direction='in', sender='customer', text=f'הודעה {index}')
            for index in range(60)
        ])
        data = self.get(f'contacts/{contact.id}/').data
        self.assertEqual(len(data['messages']), 50)
        self.assertTrue(data['has_older'])
        ids = [message['id'] for message in data['messages']]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(data['messages'][-1]['text'], 'הודעה 59')
        self.assertEqual(
            set(data['messages'][0]),
            {'id', 'direction', 'sender', 'sender_label', 'sender_name', 'text', 'message_type', 'media_url',
             'status', 'error', 'sent_at'},
        )

        older = self.get(f'contacts/{contact.id}/messages/', before=ids[0], limit=50).data
        self.assertEqual(len(older['messages']), 11)
        self.assertFalse(older['has_older'])
        self.assertEqual(older['messages'][0]['text'], 'פתיחה')

    def test_new_messages_after_an_id(self):
        self.incoming('ראשונה')
        contact = self.contact()
        last = Message.objects.get().id
        self.assertEqual(self.get(f'contacts/{contact.id}/messages/', after=last).data['messages'], [])
        self.bot_reply('תשובה')
        self.incoming('שנייה')
        data = self.get(f'contacts/{contact.id}/messages/', after=last).data
        self.assertEqual([message['text'] for message in data['messages']], ['תשובה', 'שנייה'])
        self.assertTrue(data['has_older'])
        self.assertEqual(data['messages'][0]['sender_label'], 'בוט')

    def test_events_newest_first(self):
        self.incoming('נציג בבקשה')
        data = self.get(f'contacts/{self.contact().id}/').data
        kinds = [event['kind'] for event in data['events']]
        self.assertEqual(kinds, ['created', 'needs_human_changed'])
        self.assertEqual(data['events'][0]['kind_label'], 'נוצר')
        self.assertIsNone(data['events'][0]['actor_name'])

    def test_renaming(self):
        self.incoming('שלום')
        contact = self.contact()
        response = self.client.patch(f'/api/v1/wahub/contacts/{contact.id}/', {'name': '  רותם   לוי '}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['name'], 'רותם לוי')
        self.assertEqual(self.client.patch(f'/api/v1/wahub/contacts/{contact.id}/', {}, format='json').status_code, 400)

    def test_adding_a_contact_by_hand(self):
        response = self.post('contacts/', {'phone': '050-555 0199', 'name': 'ליד מהטלפון', 'note': 'התקשרה למשרד'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['phone'], '972505550199')
        self.assertEqual(response.data['source'], 'manual')
        self.assertEqual(response.data['kogo']['outcome'], 'not_found')
        events = self.get(f"contacts/{response.data['id']}/").data['events']
        self.assertEqual([event['kind'] for event in events], ['note', 'created'])
        self.assertEqual(events[0]['text'], 'התקשרה למשרד')
        self.assertEqual(events[0]['actor_name'], 'דנה מנהלת')

    def test_adding_a_phone_that_exists_is_409_with_the_contact(self):
        self.incoming('שלום')
        response = self.post('contacts/', {'phone': '0505550101', 'name': 'שוב'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, {'code': 'exists', 'contact_id': self.contact().id})

    def test_adding_a_bad_phone_is_400(self):
        response = self.post('contacts/', {'phone': '12345', 'name': 'x'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'invalid_phone')

    def test_an_unknown_contact_is_404(self):
        self.assertEqual(self.get('contacts/999999/').status_code, 404)
        self.assertEqual(self.post('contacts/999999/read/').status_code, 404)


class LiveUpdateTests(WahubTestCase):
    def test_without_a_cursor_it_hands_one_out_with_the_counts(self):
        self.incoming('שלום')
        data = self.get('contacts/updates/').data
        self.assertEqual(data['contacts'], [])
        self.assertTrue(data['cursor'].endswith('Z'))
        self.assertEqual(data['boxes'], {'all': 1, 'waiting': 1, 'needs_human': 0, 'unread': 1, 'human': 0, 'bot': 1})

    def test_only_what_changed_since_the_cursor_comes_back(self):
        self.incoming('ישן', phone='972505550111')
        Contact.objects.update(touched_at=timezone.now() - timedelta(seconds=30))
        cursor = self.get('contacts/updates/').data['cursor']
        self.assertEqual(self.get('contacts/updates/', since=cursor).data['contacts'], [])

        self.incoming('חדש', phone='972505550122')
        data = self.get('contacts/updates/', since=cursor).data
        self.assertEqual([contact['phone'] for contact in data['contacts']], ['972505550122'])
        self.assertEqual(data['boxes']['all'], 2)
        self.assertNotEqual(data['cursor'], cursor)

    def test_every_kind_of_change_moves_the_contact(self):
        self.incoming('שלום')
        contact = self.contact()
        tag = self.post('tags/', {'name': 'חם', 'color': '#ff0000'}).data

        def changed_by(action):
            Contact.objects.update(touched_at=timezone.now() - timedelta(seconds=30))
            cursor = self.get('contacts/updates/').data['cursor']
            action()
            return [row['id'] for row in self.get('contacts/updates/', since=cursor).data['contacts']]

        actions = {
            'a new message': lambda: self.incoming('עוד'),
            'a bot reply': lambda: self.bot_reply('תשובה'),
            'read': lambda: self.post(f'contacts/{contact.id}/read/'),
            'follow-up': lambda: self.client.patch(
                f'/api/v1/wahub/contacts/{contact.id}/followup/', {'status': 'answered'}, format='json'),
            'tags': lambda: self.client.put(
                f'/api/v1/wahub/contacts/{contact.id}/tags/', {'tag_ids': [tag['id']]}, format='json'),
            'needs human': lambda: self.post(f'contacts/{contact.id}/needs-human/', {'needs_human': True}),
            'rename': lambda: self.client.patch(f'/api/v1/wahub/contacts/{contact.id}/', {'name': 'שם'}, format='json'),
            'a tag renamed': lambda: self.client.patch(f"/api/v1/wahub/tags/{tag['id']}/", {'name': 'חם מאוד'}, format='json'),
            'matched': lambda: self.post(f'contacts/{contact.id}/recheck/'),
            'summarised': lambda: self.post(f'contacts/{contact.id}/analyze/'),
        }
        for name, action in actions.items():
            with self.subTest(change=name):
                self.assertEqual(changed_by(action), [contact.id])

    def test_a_row_saved_just_before_the_cursor_is_still_delivered(self):
        """The two-second overlap: a save that commits late is not lost."""
        self.incoming('שלום')
        contact = self.contact()
        cursor_time = timezone.now()
        Contact.objects.update(touched_at=cursor_time - timedelta(seconds=1))
        with patch('apps.wahub.views.timezone.now', return_value=cursor_time):
            cursor = self.get('contacts/updates/').data['cursor']
        self.assertEqual([row['id'] for row in self.get('contacts/updates/', since=cursor).data['contacts']], [contact.id])

        Contact.objects.update(touched_at=cursor_time - timedelta(seconds=3))
        self.assertEqual(self.get('contacts/updates/', since=cursor).data['contacts'], [])

    def test_newest_first_and_a_burst_is_carried_over_the_next_calls(self):
        base = timezone.now() - timedelta(minutes=10)
        Contact.objects.bulk_create([
            Contact(phone=f'9725055{index:05d}', touched_at=base + timedelta(seconds=index))
            for index in range(130)
        ])
        start = (base - timedelta(seconds=5)).isoformat().replace('+00:00', 'Z')
        first = self.get('contacts/updates/', since=start).data
        self.assertEqual(len(first['contacts']), 100)
        phones = [row['phone'] for row in first['contacts']]
        self.assertEqual(phones[0], '972505500099')   # newest of the first hundred comes first
        self.assertEqual(phones[-1], '972505500000')

        second = self.get('contacts/updates/', since=first['cursor']).data
        seen = set(phones) | {row['phone'] for row in second['contacts']}
        self.assertEqual(len(seen), 130)

    def test_rows_saved_in_the_same_instant_are_never_split_between_calls(self):
        instant = timezone.now() - timedelta(minutes=10)
        Contact.objects.bulk_create([Contact(phone=f'9725055{index:05d}', touched_at=instant) for index in range(150)])
        later = Contact.objects.create(phone='972505599999')
        Contact.objects.filter(pk=later.pk).update(touched_at=instant + timedelta(seconds=30))
        start = (instant - timedelta(seconds=5)).isoformat().replace('+00:00', 'Z')

        first = self.get('contacts/updates/', since=start).data
        self.assertEqual(len(first['contacts']), 150)       # all of them, though a call is a hundred
        second = self.get('contacts/updates/', since=first['cursor']).data
        self.assertEqual([row['phone'] for row in second['contacts']], ['972505599999'])
        third = self.get('contacts/updates/', since=second['cursor']).data
        self.assertEqual(third['contacts'], [])

    def test_a_tag_renamed_on_many_contacts_reaches_every_one_of_them_a_hundred_a_call(self):
        from apps.wahub.models import Tag

        tag = Tag.objects.create(name='חם')
        old = timezone.now() - timedelta(minutes=10)
        contacts = Contact.objects.bulk_create([Contact(phone=f'9725055{index:05d}', touched_at=old) for index in range(230)])
        tag.contacts.set(contacts)
        cursor = self.get('contacts/updates/').data['cursor']

        self.client.patch(f'/api/v1/wahub/tags/{tag.id}/', {'name': 'חם מאוד'}, format='json')
        seen, calls = set(), 0
        while True:
            data = self.get('contacts/updates/', since=cursor).data
            calls += 1
            new = {row['id'] for row in data['contacts']} - seen
            if not new:
                break
            self.assertLessEqual(len(data['contacts']), 100)
            self.assertTrue(all(row['tags'][0]['name'] == 'חם מאוד' for row in data['contacts']))
            seen |= new
            cursor = data['cursor']
            self.assertLess(calls, 10)
        self.assertEqual(len(seen), 230)

    def test_a_cursor_that_cannot_be_read_behaves_like_no_cursor(self):
        self.incoming('שלום')
        data = self.get('contacts/updates/', since='yesterday').data
        self.assertEqual(data['contacts'], [])
        self.assertIn('cursor', data)

    def test_the_update_is_a_fixed_handful_of_queries(self):
        for index in range(12):
            self.incoming('שלום', phone=f'9725055501{index:02d}')
        cursor = (timezone.now() - timedelta(minutes=1)).isoformat().replace('+00:00', 'Z')
        # The user, the profile; the touched rows, their tags; the counts.
        with self.assertNumQueries(5):
            data = self.get('contacts/updates/', since=cursor).data
        self.assertEqual(len(data['contacts']), 12)

    def test_the_touched_rows_are_found_through_the_index(self):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'wahub_contacts' AND indexdef LIKE '%(touched_at)%'"
            )
            self.assertEqual(len(cursor.fetchall()), 1)
