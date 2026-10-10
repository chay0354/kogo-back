"""The marks a person fills in, the tags, and the ready-made replies."""
from datetime import timedelta

from apps.wahub import state
from apps.wahub.models import Contact, ContactEvent, QuickReply, Tag
from apps.wahub.tests.base import WahubTestCase


class FollowupTests(WahubTestCase):
    def setUp(self):
        super().setUp()
        self.incoming('שלום')
        self.contact_id = self.contact().id
        self.today = state.now_israel_date()

    def mark(self, **data):
        return self.client.patch(f'/api/v1/wahub/contacts/{self.contact_id}/followup/', data, format='json')

    def test_a_mark_is_saved_with_who_and_when(self):
        response = self.mark(status='no_answer', note='לא ענתה פעמיים')
        self.assertEqual(response.status_code, 200)
        followup = response.data['followup']
        self.assertEqual(followup['status'], 'no_answer')
        self.assertEqual(followup['status_label'], 'לא ענה')
        self.assertEqual(followup['note'], 'לא ענתה פעמיים')
        self.assertEqual(followup['by_name'], 'דנה מנהלת')
        self.assertIsNotNone(followup['at'])
        event = ContactEvent.objects.filter(contact_id=self.contact_id, kind='followup_changed').get()
        self.assertEqual(event.actor, self.manager)
        self.assertIn('לא ענה', event.text)

    def test_due_is_kept_only_under_another_time(self):
        due = (self.today + timedelta(days=14)).isoformat()
        self.assertEqual(self.mark(status='later', due=due).data['followup']['due'], due)
        # A note alone leaves the date where it is.
        self.assertEqual(self.mark(note='אחרי החגים').data['followup']['due'], due)
        # Any other mark clears it, even when a date is sent along.
        self.assertIsNone(self.mark(status='answered', due=due).data['followup']['due'])
        self.assertIsNone(Contact.objects.get(pk=self.contact_id).followup_due)
        # And a date sent with no "another time" is not kept either.
        self.assertIsNone(self.mark(due=due).data['followup']['due'])

    def test_the_date_can_be_removed(self):
        due = (self.today + timedelta(days=3)).isoformat()
        self.mark(status='later', due=due)
        self.assertIsNone(self.mark(due=None).data['followup']['due'])
        self.assertEqual(Contact.objects.get(pk=self.contact_id).followup_status, 'later')

    def test_a_mark_can_be_cleared(self):
        self.mark(status='answered')
        response = self.mark(status='')
        self.assertEqual(response.data['followup']['status'], '')
        self.assertEqual(response.data['followup']['status_label'], '')

    def test_refused_values(self):
        self.assertEqual(self.mark(status='maybe').status_code, 400)
        self.assertEqual(self.mark(status='later', due='14/10/2026').status_code, 400)
        self.assertEqual(self.mark().status_code, 400)
        self.assertEqual(Contact.objects.get(pk=self.contact_id).followup_status, '')

    def test_saving_the_same_thing_again_writes_nothing(self):
        self.mark(status='answered')
        before = Contact.objects.get(pk=self.contact_id)
        self.mark(status='answered')
        after = Contact.objects.get(pk=self.contact_id)
        self.assertEqual(before.followup_at, after.followup_at)
        self.assertEqual(ContactEvent.objects.filter(contact_id=self.contact_id, kind='followup_changed').count(), 1)

    def is_due(self) -> bool:
        return self.get(f'contacts/{self.contact_id}/').data['followup']['is_due']

    def test_is_due_waiting_for_us(self):
        self.assertFalse(self.is_due())
        self.mark(status='waiting_us')
        self.assertTrue(self.is_due())

    def test_is_due_another_time_when_its_day_comes(self):
        self.mark(status='later', due=(self.today + timedelta(days=1)).isoformat())
        self.assertFalse(self.is_due())
        self.mark(status='later', due=self.today.isoformat())
        self.assertTrue(self.is_due())
        self.mark(status='later', due=(self.today - timedelta(days=5)).isoformat())
        self.assertTrue(self.is_due())
        self.mark(status='later', due=None)
        self.assertFalse(self.is_due())

    def test_is_due_from_what_the_customer_said_until_somebody_closes_it(self):
        Contact.objects.filter(pk=self.contact_id).update(known_callback_on=self.today)
        for status, expected in (
            ('', True), ('later', True), ('answered', True), ('no_answer', True),
            ('registered', False), ('not_relevant', False), ('waiting_us', True),
        ):
            with self.subTest(status=status):
                self.mark(status=status)
                self.assertEqual(self.is_due(), expected)
        Contact.objects.filter(pk=self.contact_id).update(
            known_callback_on=self.today + timedelta(days=1), followup_status='',
        )
        self.assertFalse(self.is_due())

    def test_the_due_queue_and_the_flag_agree(self):
        Contact.objects.filter(pk=self.contact_id).update(known_callback_on=self.today - timedelta(days=1))
        listed = self.get('contacts/', view='leads', queue='due').data['results']
        self.assertEqual([row['id'] for row in listed], [self.contact_id])
        self.assertTrue(listed[0]['followup']['is_due'])
        self.assertEqual(self.get('contacts/counts/').data['queues']['due'], 1)
        self.assertEqual(self.get('summary/').data['due_followups'], 1)


class TagTests(WahubTestCase):
    def test_create_list_rename_delete(self):
        created = self.post('tags/', {'name': ' ראש העין ', 'color': '#22AA55'})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.data['name'], 'ראש העין')
        self.assertEqual(created.data['color'], '#22aa55')
        self.assertEqual(set(created.data), {'id', 'name', 'color'})
        self.post('tags/', {'name': 'אחרי החגים', 'color': '#0000ff'})

        listed = self.get('tags/')
        self.assertIsInstance(listed.data, list)
        self.assertEqual([tag['name'] for tag in listed.data], ['אחרי החגים', 'ראש העין'])

        renamed = self.client.patch(f"/api/v1/wahub/tags/{created.data['id']}/", {'name': 'ראש העין - פסגות'}, format='json')
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.data['name'], 'ראש העין - פסגות')

        self.assertEqual(self.client.delete(f"/api/v1/wahub/tags/{created.data['id']}/").status_code, 204)
        self.assertEqual(Tag.objects.count(), 1)

    def test_refused_tags_answer_with_one_line(self):
        self.post('tags/', {'name': 'חם', 'color': '#ff0000'})
        for body in ({'name': 'חם', 'color': '#00ff00'}, {'name': '', 'color': '#00ff00'}, {'name': 'x', 'color': 'red'}):
            response = self.post('tags/', body)
            self.assertEqual(response.status_code, 400)
            self.assertIsInstance(response.data['detail'], str)

    def test_a_tag_with_no_color_gets_the_default(self):
        self.assertEqual(self.post('tags/', {'name': 'בלי צבע'}).data['color'], '#64748b')

    def test_setting_a_contacts_tags(self):
        self.incoming('שלום')
        contact = self.contact()
        hot = Tag.objects.create(name='חם', color='#ff0000')
        far = Tag.objects.create(name='רחוק', color='#999999')
        url = f'/api/v1/wahub/contacts/{contact.id}/tags/'

        response = self.client.put(url, {'tag_ids': [hot.id, far.id]}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['tags'], [
            {'id': hot.id, 'name': 'חם', 'color': '#ff0000'}, {'id': far.id, 'name': 'רחוק', 'color': '#999999'},
        ])
        response = self.client.put(url, {'tag_ids': [far.id]}, format='json')
        self.assertEqual([tag['name'] for tag in response.data['tags']], ['רחוק'])
        events = list(ContactEvent.objects.filter(contact=contact, kind='tags_changed').values_list('text', flat=True))
        self.assertEqual(events, ['הוסרו: חם', 'נוספו: חם, רחוק'])
        self.assertEqual(self.client.put(url, {'tag_ids': []}, format='json').data['tags'], [])

        self.assertEqual(self.client.put(url, {'tag_ids': [999999]}, format='json').status_code, 400)
        self.assertEqual(self.client.put(url, {'tag_ids': 'חם'}, format='json').status_code, 400)
        self.assertEqual(self.client.put(url, {}, format='json').status_code, 400)

    def test_deleting_a_tag_takes_it_off_its_contacts(self):
        self.incoming('שלום')
        contact = self.contact()
        tag = Tag.objects.create(name='חם')
        contact.tags.add(tag)
        self.client.delete(f'/api/v1/wahub/tags/{tag.id}/')
        self.assertEqual(self.get(f'contacts/{contact.id}/').data['tags'], [])


class QuickReplyTests(WahubTestCase):
    def test_create_list_edit_delete(self):
        created = self.post('quick-replies/', {'title': 'פתיחה', 'text': 'היי {{first_name}}, כאן קוגומלו'})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(set(created.data), {'id', 'title', 'text'})
        self.post('quick-replies/', {'title': 'אחרי ניסיון', 'text': 'איך היה השיעור?'})

        listed = self.get('quick-replies/')
        self.assertIsInstance(listed.data, list)
        self.assertEqual([reply['title'] for reply in listed.data], ['פתיחה', 'אחרי ניסיון'])   # as written
        # The placeholder is the screen's to fill; the server keeps it as written.
        self.assertIn('{{first_name}}', listed.data[0]['text'])

        url = f"/api/v1/wahub/quick-replies/{created.data['id']}/"
        self.assertEqual(self.client.patch(url, {'text': 'שלום!'}, format='json').data['text'], 'שלום!')
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertEqual(QuickReply.objects.count(), 1)

    def test_refused(self):
        self.assertEqual(self.post('quick-replies/', {'title': '', 'text': 'x'}).status_code, 400)
        self.assertEqual(self.post('quick-replies/', {'title': 'x', 'text': '  '}).status_code, 400)
        self.assertEqual(self.post('quick-replies/', {'title': 'x', 'text': 'א' * 4097}).status_code, 400)
