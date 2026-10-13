"""
The addition of 13.10.2026, noon, to the waiting list (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ז):
the whole list grouped by lesson with the lesson's seats, the lessons a person
chooses from, and — the one thing here that sends — telling a customer that a
seat came free, on a person's press and never by itself.

The seats are the registration form's own count throughout (test_waiting_list's
fixture: a lesson gets a limit and that many paying children). ManyChat is the
stand-in of test_lead_automation: every call the client would make is seen, and
nothing leaves the process (base.py cuts the network).
"""
from datetime import timedelta
from unittest.mock import patch

import requests
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.manychat_service import ManyChatError
from apps.core.models import UserProfile
from apps.wahub import lead_automation, lead_followup_seed, lead_followups, lead_templates, sending, settings_store, waiting_list, waiting_notify
from apps.wahub.models import Contact, ContactEvent, KnowledgeItem, Message, WaitingListEntry
from apps.wahub.tests.base import BASE, client_for, make_user
from apps.wahub.tests.test_lead_automation import ManyChat
from apps.wahub.tests.test_lead_followups import REQUEST
from apps.wahub.tests.test_waiting_list import FREED, LABEL, WaitingListCase
from apps.wahub.trial_offer import Seats

# The door every send goes through, as it really is: the fixture swaps it for one that fails the test.
REAL_DELIVER = sending._deliver

SENDING_ON = dict(MANYCHAT_KEY='test-key', WAHUB_SENDING_ENABLED=True)
SENDING_OFF = dict(MANYCHAT_KEY='test-key', WAHUB_SENDING_ENABLED=False)
WORDS = 'היי התפנה מקום ב *קפוארה 4.5-6 יום שלישי* ביום שלישי ב-17:00 (קרל וגרטי קורי 8) 🎉 רוצים שנשמור לכם אותו?'
SEAT_FLOW = 'auto_kogo_lead_seat_1'
TOLD = 'נשלחה ללקוח הודעה שהתפנה מקום'


# --- the whole list, by lesson ----------------------------------------------------------------------------

class WholeListTests(WaitingListCase):
    def test_the_rows_are_grouped_by_lesson_with_its_seats_as_they_are_now(self):
        self.fill(self.dance_lesson)
        self.fill(seats=2)
        other = Contact.objects.create(phone='972505550202', name='שני')
        first = self.join().data['entry']['id']
        second = self.join(other).data['entry']['id']
        third = self.join(other, self.dance_lesson).data['entry']['id']

        data = self.get('waiting-list/').data
        self.assertEqual([row['id'] for row in data['results']], [first, second, third])
        capoeira, dance = data['lessons']
        self.assertEqual(set(capoeira), {
            'id', 'course_id', 'course', 'day', 'start', 'end', 'branch', 'label', 'exists', 'enrolled', 'capacity',
            'seats_left', 'is_full', 'seats_label', 'waiting', 'freed', 'entry_ids',
        })
        self.assertEqual((capoeira['id'], capoeira['label'], capoeira['exists']), (str(self.young_lesson.id), LABEL, True))
        self.assertEqual((capoeira['enrolled'], capoeira['capacity'], capoeira['seats_left'], capoeira['is_full']), (2, 2, 0, True))
        self.assertEqual(capoeira['seats_label'], '2 מתוך 2 · מלא')
        self.assertEqual((capoeira['waiting'], capoeira['freed'], capoeira['entry_ids']), (2, 0, [first, second]))
        self.assertEqual((dance['id'], dance['seats_label'], dance['entry_ids']), (str(self.dance_lesson.id), '1 מתוך 1 · מלא', [third]))

        # A seat comes free in the dance lesson: it is first, and its numbers are the ones of this minute.
        in_the_dance_lesson = self.sitting.pop(0)
        in_the_dance_lesson.status = 'cancelled'
        in_the_dance_lesson.save(update_fields=['status'])
        waiting_list.check()
        data = self.get('waiting-list/').data
        self.assertEqual([lesson['id'] for lesson in data['lessons']], [str(self.dance_lesson.id), str(self.young_lesson.id)])
        dance = data['lessons'][0]
        self.assertEqual((dance['enrolled'], dance['capacity'], dance['seats_left'], dance['is_full']), (0, 1, 1, False))
        self.assertEqual((dance['seats_label'], dance['waiting'], dance['freed']), ('0 מתוך 1 · נשאר מקום אחד', 0, 1))
        # What the form itself counts for that lesson, asked the form's way.
        cap = Seats([self.dancers]).cap(self.dance_lesson, self.dancers)
        self.assertEqual((dance['enrolled'], dance['capacity'], dance['seats_left']), (cap['enrolled_count'], cap['capacity'], cap['available_spots']))

    def test_a_filter_narrows_the_groups_but_not_what_they_count(self):
        self.fill()
        other = Contact.objects.create(phone='972505550202', name='שני')
        first = self.join().data['entry']['id']
        second = self.join(other).data['entry']['id']
        WaitingListEntry.objects.filter(pk=second).update(status='freed', freed_at=timezone.now())
        lessons = self.get('waiting-list/', status='freed').data['lessons']
        self.assertEqual([(lesson['entry_ids'], lesson['waiting'], lesson['freed']) for lesson in lessons], [([second], 1, 1)])
        self.assertEqual(self.get('waiting-list/', status='closed').data['lessons'], [])
        self.assertEqual(self.get('waiting-list/', lesson='x').data['lessons'], [])
        self.assertEqual(self.get('waiting-list/').data['lessons'][0]['entry_ids'], [first, second])   # the queue, oldest first

    def test_a_lesson_that_is_gone_is_shown_from_the_copy_with_no_numbers(self):
        self.fill()
        self.join()
        self.young_lesson.status = 'cancelled'
        self.young_lesson.save(update_fields=['status'])
        lesson = self.get('waiting-list/').data['lessons'][0]
        self.assertEqual((lesson['exists'], lesson['label']), (False, LABEL))
        self.assertEqual((lesson['enrolled'], lesson['capacity'], lesson['seats_left'], lesson['is_full'], lesson['seats_label']), (None, None, None, False, ''))

    def test_a_row_still_waiting_and_one_whose_seat_came_free_are_both_taken_off(self):
        """ז.3: "להסיר מהרשימה" works on every row that is on the list, whatever its state."""
        self.fill()
        other = Contact.objects.create(phone='972505550202', name='שני')
        waiting = self.join().data['entry']['id']
        freed = self.join(other).data['entry']['id']
        WaitingListEntry.objects.filter(pk=freed).update(status='freed', freed_at=timezone.now())
        waiting_list.sync_contact(other.pk)
        for entry_id, contact in ((waiting, self.lead), (freed, other)):
            with self.subTest(entry=entry_id):
                response = self.post(f'waiting-list/{entry_id}/remove/')
                self.assertEqual((response.status_code, response.data['entry']['status']), (200, 'removed'))
                self.assertEqual(response.data['entry']['closed_by_name'], 'דנה מנהלת')
                self.assertIsNone(response.data['contact']['waiting_list'])
                self.assertEqual(self.fresh(contact).lead_waiting, {})
        self.assertEqual(self.get('waiting-list/').data, {'counts': {'waiting': 0, 'freed': 0}, 'lessons': [], 'results': []})


# --- the lessons a person chooses from --------------------------------------------------------------------

class LessonsToChooseTests(WaitingListCase):
    def lessons(self, **params) -> list:
        response = self.get('waiting-list/lessons/', **params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data['results']

    def test_every_lesson_somebody_can_wait_for_with_its_seats(self):
        self.fill()
        data = self.get('waiting-list/lessons/').data
        rows = data['results']
        self.assertEqual(data['total'], len(rows))
        self.assertEqual(data['branches'], [{'id': str(self.branch.id), 'name': 'קרל וגרטי קורי 8'}])
        # Ours only, by class, then day and hour: no lesson of the external branch.
        self.assertEqual([row['id'] for row in rows], [
            str(self.dance_lesson.id), str(self.older_sunday.id), str(self.older_wednesday.id), str(self.young_lesson.id),
        ])
        full = rows[-1]
        self.assertEqual(set(full), {
            'id', 'course_id', 'course', 'day', 'start', 'end', 'branch_id', 'branch', 'label', 'enrolled', 'capacity',
            'seats_left', 'is_full', 'seats_label', 'can_join', 'waiting', 'on_list',
        })
        self.assertEqual((full['label'], full['course'], full['day'], full['start'], full['end']), (LABEL, 'קפוארה 4.5-6 יום שלישי', 'שלישי', '17:00', '17:45'))
        self.assertEqual((full['enrolled'], full['capacity'], full['seats_left'], full['is_full'], full['seats_label']), (1, 1, 0, True, '1 מתוך 1 · מלא'))
        self.assertEqual((full['can_join'], full['waiting'], full['on_list']), (True, 0, False))
        # The numbers of every row are the form's own.
        courses = {str(course.id): course for course in (self.young, self.older, self.dancers)}
        lessons = {str(lesson.id): lesson for lesson in (self.young_lesson, self.older_sunday, self.older_wednesday, self.dance_lesson)}
        seats = Seats(list(courses.values()))
        for row in rows:
            with self.subTest(lesson=row['label']):
                cap = seats.cap(lessons[row['id']], courses[row['course_id']])
                self.assertEqual(
                    (row['enrolled'], row['capacity'], row['seats_left'], row['is_full']),
                    (cap['enrolled_count'], cap['capacity'], cap['available_spots'], cap['is_full']),
                )
                self.assertEqual(row['can_join'], cap['is_full'])

    def test_a_lesson_is_offered_exactly_when_the_office_can_put_somebody_on_its_list(self):
        """`can_join` and the door itself (POST contacts/{id}/waiting-list/) never disagree — the office too waits only for a full lesson."""
        self.fill()
        for row in self.lessons():
            with self.subTest(lesson=row['label']):
                response = self.post(f'contacts/{self.lead.id}/waiting-list/', {'lesson_id': row['id']})
                self.assertEqual(response.status_code, 201 if row['can_join'] else 400)
                if not row['can_join']:
                    self.assertEqual(response.data['code'], 'has_room')
        self.assertEqual(WaitingListEntry.objects.count(), 1)

    def test_the_seats_in_the_screens_words(self):
        self.fill(seats=3)
        self.sitting.pop().delete()
        by_id = {row['id']: row for row in self.lessons()}
        self.assertEqual(by_id[str(self.young_lesson.id)]['seats_label'], '2 מתוך 3 · נשאר מקום אחד')
        self.sitting.pop().delete()
        by_id = {row['id']: row for row in self.lessons()}
        self.assertEqual(by_id[str(self.young_lesson.id)]['seats_label'], '1 מתוך 3 · נשארו 2')
        self.assertFalse(by_id[str(self.young_lesson.id)]['can_join'])
        # No limit anywhere — no room, no group, no lesson: never full, and nobody can wait for it.
        self.assertEqual(waiting_list.seats_block({'enrolled_count': 12, 'capacity': None, 'available_spots': None, 'is_full': False}), {
            'enrolled': 12, 'capacity': None, 'seats_left': None, 'is_full': False, 'seats_label': '12 רשומים · בלי מגבלת מקומות',
        })
        self.assertEqual(waiting_list.seats_block({'enrolled_count': 0, 'capacity': None, 'available_spots': None, 'is_full': False})['seats_label'], 'אין רשומים · בלי מגבלת מקומות')

    def test_searching_by_words_by_branch_and_only_what_is_full(self):
        self.fill()
        self.assertEqual([row['id'] for row in self.lessons(q='קפוארה שלישי')], [str(self.young_lesson.id)])
        self.assertEqual([row['id'] for row in self.lessons(q='ריקוד')], [str(self.dance_lesson.id)])
        self.assertEqual([row['id'] for row in self.lessons(q='ראש העין 17:00')], [str(self.dance_lesson.id), str(self.young_lesson.id)])
        self.assertEqual(self.lessons(q='שחייה'), [])
        self.assertEqual([row['id'] for row in self.lessons(full='1')], [str(self.young_lesson.id)])
        self.assertEqual(len(self.lessons(branch=str(self.branch.id))), 4)
        elsewhere = self.get('waiting-list/lessons/', branch=str(self.external.id)).data
        self.assertEqual((elsewhere['results'], [branch['name'] for branch in elsewhere['branches']]), ([], ['קרל וגרטי קורי 8']))
        for bad in ({'branch': 'מינץ'}, {'contact': 'x'}):
            with self.subTest(bad=bad):
                response = self.get('waiting-list/lessons/', **bad)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.data['detail'])

    def test_what_nobody_can_be_registered_to_is_not_offered(self):
        self.older_sunday.status = 'cancelled'
        self.older_sunday.save(update_fields=['status'])
        self.dancers.is_active = False
        self.dancers.save(update_fields=['is_active'])
        self.assertEqual([row['id'] for row in self.lessons()], [str(self.older_wednesday.id), str(self.young_lesson.id)])

    def test_who_already_waits_for_a_lesson(self):
        self.fill()
        other = Contact.objects.create(phone='972505550202', name='שני')
        self.join()
        self.join(other)
        mine = {row['id']: row for row in self.lessons(contact=self.lead.id)}
        self.assertEqual((mine[str(self.young_lesson.id)]['on_list'], mine[str(self.young_lesson.id)]['waiting']), (True, 2))
        self.assertFalse(mine[str(self.dance_lesson.id)]['on_list'])
        self.assertFalse({row['id']: row for row in self.lessons()}[str(self.young_lesson.id)]['on_list'])

    def test_it_reads_and_writes_nothing(self):
        self.fill()
        before = (WaitingListEntry.objects.count(), ContactEvent.objects.count(), self.fresh().touched_at)
        self.lessons(contact=self.lead.id, q='קפוארה')
        self.assertEqual((WaitingListEntry.objects.count(), ContactEvent.objects.count(), self.fresh().touched_at), before)


# --- telling the customer that a seat came free ---------------------------------------------------------

class NotifyCase(WaitingListCase):
    """A lead on the list of a full lesson, a seat that came free — and ManyChat standing behind the real door."""

    def setUp(self):
        super().setUp()
        self.deliver.side_effect = REAL_DELIVER                # sending.py as it is; ManyChat itself is the stand-in below
        self.manychat = ManyChat(timezone.now)
        self.door = patch(REQUEST, side_effect=self.manychat)
        self.door.start()
        Contact.objects.filter(pk=self.lead.pk).update(manychat_subscriber_id='4242')
        self.fill()
        self.entry_id = self.join(child='בן 5').data['entry']['id']
        self.free_a_seat()
        waiting_list.check()
        self.messages = Message.objects.filter(contact=self.lead).count()

    def entry(self) -> WaitingListEntry:
        return WaitingListEntry.objects.get(pk=self.entry_id)

    def look(self, entry_id=None):
        return self.get(f'waiting-list/{entry_id or self.entry_id}/notify/')

    def press(self, entry_id=None, **body):
        return self.post(f'waiting-list/{entry_id or self.entry_id}/notify/', body)

    def out(self, contact=None) -> list:
        """What was written into the conversation by us since the seat came free: (type, status, text)."""
        return list(
            Message.objects.filter(contact=contact or self.lead, direction='out', source='kogo').order_by('id')
            .values_list('message_type', 'status', 'text')
        )

    def nothing_written(self):
        self.assertEqual(self.manychat.sends, 0)
        self.assertEqual(self.out(), [])
        self.assertEqual((self.entry().notified_at, self.entry().notified_by, self.entry().notified_how), (None, None, ''))
        self.assertIsNone(self.entry().notify_claimed_at)
        self.assertEqual([line for line in self.journal() if TOLD in line], [])


class WordsTests(NotifyCase):
    def test_the_words_name_the_lesson_of_the_row(self):
        self.assertEqual(waiting_notify.words(self.entry()), WORDS)
        # Short, warm, plural, no comma after "היי", the class in WhatsApp's bold with a space before it.
        self.assertTrue(WORDS.startswith('היי התפנה מקום ב *'))
        for unwanted in ('היי,', 'אשמח', 'נשמח', '—', 'ב*', '{', '['):
            self.assertNotIn(unwanted, WORDS)
        self.assertEqual(WORDS.count('?'), 1)

    def test_a_part_of_the_lesson_that_is_not_known_is_left_out(self):
        WaitingListEntry.objects.filter(pk=self.entry_id).update(branch_name='', day_of_week=None)
        self.assertEqual(waiting_notify.words(self.entry()), 'היי התפנה מקום ב *קפוארה 4.5-6 יום שלישי* ב-17:00 🎉 רוצים שנשמור לכם אותו?')

    def test_the_owner_rewrites_the_words_on_the_bot_screen(self):
        lead_followup_seed.run()
        item = KnowledgeItem.objects.get(key='lead_seat_freed')
        self.assertEqual((item.kind, item.is_active, item.when_to_say), ('phrasing', True, 'internal'))
        self.assertIn('{חוג} {יום} {שעה} {סניף}', item.data['when'])
        item.body = 'היי יש מקום ב *{חוג}* 🙂 מתאים לכם?'
        item.save(update_fields=['body'])
        self.assertEqual(waiting_notify.words(self.entry()), 'היי יש מקום ב *קפוארה 4.5-6 יום שלישי* 🙂 מתאים לכם?')

    def test_the_bot_that_answers_customers_is_never_read_these_words(self):
        """It is what the office sends by a press, not what an answer may be built from."""
        from apps.wahub import knowledge

        lead_followup_seed.run()
        today = timezone.localtime(timezone.now()).date()
        self.assertNotIn('lead_seat_freed', [item.key for item in knowledge.relevant_items(today=today)])


class PreviewTests(NotifyCase):
    @override_settings(**SENDING_ON)
    def test_what_a_press_would_do_now(self):
        response = self.look()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['entry']['id'], self.entry_id)
        self.assertEqual(response.data['notify'], {
            'can': True, 'how': 'text', 'how_label': 'תצא כהודעה רגילה (הלקוח כתב ב-24 השעות האחרונות)', 'text': WORDS,
            'blocked': '', 'detail': '', 'needs_again': False, 'already_notified_at': None, 'already_notified_by_name': None,
        })
        self.nothing_written()                                   # looking sends nothing and keeps nothing

    @override_settings(**SENDING_OFF)
    def test_today_the_switch_is_off_and_it_says_so(self):
        notify = self.look().data['notify']
        self.assertEqual((notify['can'], notify['blocked'], notify['detail']), (False, 'sending_off', sending.SENDING_OFF))
        self.assertEqual(notify['text'], WORDS)                  # the words are shown all the same

    @override_settings(**SENDING_ON)
    def test_after_the_24_hours_it_is_the_template_or_nothing(self):
        self.age(self.lead, last_inbound_at=60 * 25)
        notify = self.look().data['notify']
        self.assertEqual((notify['can'], notify['how'], notify['blocked']), (False, 'template', 'no_template'))
        self.assertIn('kogo_lead_seat_1', notify['detail'])
        self.manychat.named('seat_freed_1')
        settings_store.put(settings_store.LEAD_TEMPLATES_FOUND, {})          # ManyChat is asked again
        notify = self.look().data['notify']
        self.assertEqual((notify['can'], notify['how'], notify['blocked']), (True, 'template', ''))
        self.assertIn('תבנית מאושרת', notify['how_label'])

    @override_settings(**SENDING_ON)
    def test_a_row_that_is_not_one_to_tell_about_says_why(self):
        self.fill()                                                         # the seat was taken again, before the cron looked
        self.assertEqual(self.look().data['notify']['blocked'], 'taken')
        waiting_list.check()
        notify = self.look().data['notify']
        self.assertEqual((notify['can'], notify['blocked'], notify['detail']), (False, 'not_freed', waiting_notify.DETAILS['not_freed']))
        self.post(f'waiting-list/{self.entry_id}/remove/')
        self.assertEqual(self.look().data['notify']['blocked'], 'not_active')
        self.assertEqual(self.look(999999).status_code, 404)


class TellTests(NotifyCase):
    @override_settings(**SENDING_OFF)
    def test_while_the_switch_is_off_nothing_leaves_and_nothing_is_written(self):
        """Production today: the press answers, in words, and that is all it does."""
        events = ContactEvent.objects.filter(contact=self.lead).count()
        response = self.press()
        self.assertEqual(response.status_code, 200)
        notify = response.data['notify']
        self.assertEqual((notify['sent'], notify['how'], notify['code'], notify['detail']), (False, '', 'sending_off', sending.SENDING_OFF))
        self.assertEqual((notify['text'], notify['message']), (WORDS, None))
        self.assertEqual(response.data['entry']['notified_at'], None)
        self.nothing_written()
        self.assertEqual(ContactEvent.objects.filter(contact=self.lead).count(), events)
        self.assertEqual(self.manychat.asked, [])

    @override_settings(**SENDING_ON)
    def test_inside_the_24_hours_it_leaves_as_the_offices_own_text(self):
        response = self.press()
        self.assertEqual(response.status_code, 200)
        notify = response.data['notify']
        self.assertEqual((notify['sent'], notify['how'], notify['code'], notify['detail']), (True, 'text', 'sent_text', 'נשלח כהודעה רגילה'))
        self.assertEqual((notify['already_notified_at'], notify['already_notified_by_name']), (None, None))
        # One message, through ManyChat, with exactly these words.
        self.assertEqual([sent[1:] for sent in self.manychat.sent], [('text', WORDS)])
        message = notify['message']
        self.assertEqual((message['status'], message['message_type'], message['text']), ('sent', 'text', WORDS))
        self.assertEqual((message['sender'], message['sender_name']), ('office', 'דנה מנהלת'))
        self.assertEqual(self.out(), [('text', 'sent', WORDS)])
        # The row says when, by whom and how.
        entry = response.data['entry']
        self.assertEqual((entry['status'], entry['notified_by_name'], entry['notified_how'], entry['notified_how_label']), ('freed', 'דנה מנהלת', 'text', 'הודעה רגילה'))
        self.assertIsNotNone(entry['notified_at'])
        self.assertIsNone(entry['customer_replied_at'])
        self.assertEqual((self.entry().notified_by, self.entry().notify_claimed_at), (self.manager, None))
        self.assertEqual(self.journal()[-1], f'{TOLD} (הודעה רגילה): {LABEL}')
        self.assertEqual(ContactEvent.objects.filter(contact=self.lead, kind='waiting_list').latest('id').actor, self.manager)
        # The conversation is nobody's more than it was: the bot was not muted, and nobody is asked for.
        contact = response.data['contact']
        self.assertEqual((contact['chat']['handled_by'], contact['chat']['open'], contact['chat']['needs_human']), ('bot', False, False))
        self.assertEqual(contact['waiting_list']['notified_at'], entry['notified_at'])
        self.assertEqual(self.get('contacts/counts/').data['boxes']['needs_human'], 0)

    @override_settings(**SENDING_ON)
    def test_the_lead_says_the_customer_was_told_and_then_that_they_answered(self):
        self.assertEqual(self.get(f'leads/{self.lead.id}/').data['next']['what'], 'להציע ללקוח את המקום שהתפנה')
        self.press()
        row = self.get(f'leads/{self.lead.id}/').data
        self.assertEqual((row['stage'], row['owner'], row['next']['who']), ('waiting_list', 'office', 'office'))
        self.assertEqual(row['next']['what'], 'נשלחה ללקוח הודעה על המקום שהתפנה — מחכים לתשובה')
        self.assertEqual(row['waiting_list']['title'], FREED)

        with patch('django.utils.timezone.now', return_value=timezone.now() + timedelta(minutes=30)):
            self.incoming('כן! נשמח')
            from apps.wahub import lead_stage

            lead_stage.refresh(self.lead.pk)
            row = self.get(f'leads/{self.lead.id}/').data
            self.assertEqual((row['owner'], row['next']['what']), ('office', 'הלקוח ענה להודעה על המקום שהתפנה — לענות לו'))
            self.assertIn(self.lead.id, [lead['id'] for lead in self.get('leads/', owner='office').data['results']])
            listed = self.get('waiting-list/').data['results'][0]
            self.assertIsNotNone(listed['customer_replied_at'])
            # An answer comes in like any message: unread, waiting — and open only by the rules there already are.
            chat = self.get(f'contacts/{self.lead.id}/').data['chat']
            self.assertEqual((chat['open'], chat['unread_count'] > 0), (False, True))
            self.assertEqual(self.get('contacts/counts/').data['boxes']['needs_human'], 0)

    @override_settings(**SENDING_ON)
    def test_after_the_24_hours_it_leaves_as_the_automation_named_for_it(self):
        self.age(self.lead, last_inbound_at=60 * 25)
        self.manychat.named('seat_freed_1')
        response = self.press()
        notify = response.data['notify']
        self.assertEqual((notify['sent'], notify['how'], notify['code']), (True, 'template', 'sent_template'))
        self.assertEqual(notify['detail'], 'נשלח כתבנית מאושרת')
        self.assertEqual([sent[1:] for sent in self.manychat.sent], [('template', SEAT_FLOW)])
        # The conversation shows our words; the customer got the template's own.
        self.assertEqual(self.out(), [('template', 'sent', WORDS)])
        self.assertEqual((response.data['entry']['notified_how'], response.data['entry']['notified_how_label']), ('template', 'תבנית'))
        self.assertEqual(self.journal()[-1], f'{TOLD} (תבנית): {LABEL}')

    @override_settings(**SENDING_ON)
    def test_a_person_connects_another_automation_on_the_settings_card(self):
        self.age(self.lead, last_inbound_at=60 * 25)
        settings_store.set_lead_template('seat_freed_1', 'content_of_his_own', name='מקום התפנה', user=self.manager)
        self.assertTrue(self.press().data['notify']['sent'])
        self.assertEqual([sent[1:] for sent in self.manychat.sent], [('template', 'content_of_his_own')])

    @override_settings(**SENDING_ON)
    def test_after_the_24_hours_with_no_template_nothing_leaves(self):
        self.age(self.lead, last_inbound_at=60 * 25)
        notify = self.press().data['notify']
        self.assertEqual((notify['sent'], notify['code'], notify['detail']), (False, 'no_template', waiting_notify.DETAILS['no_template']))
        self.nothing_written()

    @override_settings(**SENDING_ON)
    def test_somebody_who_never_wrote_gets_the_template_only(self):
        """A contact the office added by hand: WhatsApp's 24 hours never began."""
        Contact.objects.filter(pk=self.lead.pk).update(last_inbound_at=None)
        self.assertEqual((self.look().data['notify']['how'], self.press().data['notify']['code']), ('template', 'no_template'))
        self.manychat.named('seat_freed_1')
        settings_store.put(settings_store.LEAD_TEMPLATES_FOUND, {})
        self.assertEqual(self.press().data['notify']['code'], 'sent_template')

    @override_settings(MANYCHAT_KEY='', WAHUB_SENDING_ENABLED=True)
    def test_with_no_manychat_to_send_through_nothing_leaves(self):
        notify = self.press().data['notify']
        self.assertEqual((notify['sent'], notify['code']), (False, 'no_manychat'))
        self.nothing_written()

    @override_settings(**SENDING_ON)
    def test_a_demo_contact_never_gets_anything(self):
        Contact.objects.filter(pk=self.lead.pk).update(is_demo=True)
        notify = self.press().data['notify']
        self.assertEqual((notify['sent'], notify['code'], notify['detail']), (False, 'demo', sending.DEMO_CONTACT))
        self.nothing_written()
        self.assertEqual((self.look().data['notify']['blocked'], self.look().data['notify']['text']), ('demo', WORDS))

    @override_settings(**SENDING_OFF)
    def test_the_owners_own_number_under_the_live_test_gets_it_with_the_switch_off(self):
        Contact.objects.filter(pk=self.lead.pk).update(live_test=True)
        self.assertTrue(self.press().data['notify']['sent'])
        self.assertEqual([sent[1:] for sent in self.manychat.sent], [('text', WORDS)])

    @override_settings(**SENDING_ON)
    def test_the_words_turned_off_are_not_sent(self):
        lead_followup_seed.run()
        KnowledgeItem.objects.filter(key='lead_seat_freed').update(is_active=False)
        notify = self.press().data['notify']
        self.assertEqual((notify['sent'], notify['code'], notify['text']), (False, 'no_words', ''))
        self.nothing_written()


class TwiceTests(NotifyCase):
    @override_settings(**SENDING_ON)
    def test_a_second_press_does_not_send_until_it_says_again(self):
        first = self.press().data['entry']
        response = self.press()
        self.assertEqual((response.status_code, response.data['code']), (409, 'already_notified'))
        self.assertIn('כבר נשלחה ללקוח הודעה על המקום הזה ב-', response.data['detail'])
        self.assertIn('(דנה מנהלת). לשלוח שוב?', response.data['detail'])
        self.assertEqual((response.data['already_notified_at'], response.data['already_notified_by_name']), (first['notified_at'], 'דנה מנהלת'))
        self.assertEqual((response.data['entry']['id'], response.data['contact']['id']), (self.entry_id, self.lead.id))
        self.assertEqual(self.manychat.sends, 1)
        self.assertTrue(self.look().data['notify']['needs_again'])

        again = self.press(again=True)
        self.assertEqual(again.status_code, 200)
        notify = again.data['notify']
        self.assertEqual((notify['sent'], notify['already_notified_at'], notify['already_notified_by_name']), (True, first['notified_at'], 'דנה מנהלת'))
        self.assertEqual(self.manychat.sends, 2)
        self.assertEqual(len([line for line in self.journal() if TOLD in line]), 2)

    @override_settings(**SENDING_ON)
    def test_two_presses_in_the_same_moment_send_one_message(self):
        """The first marks the row before ManyChat is asked; the second finds the mark."""
        WaitingListEntry.objects.filter(pk=self.entry_id).update(notify_claimed_at=timezone.now() - timedelta(seconds=20))
        response = self.press()
        self.assertEqual((response.status_code, response.data['code'], response.data['detail']), (409, 'in_progress', waiting_notify.DETAILS['in_progress']))
        self.assertEqual(self.manychat.sends, 0)
        # A mark nobody gave back — a call cut in the middle — goes stale, and the row can be pressed again.
        WaitingListEntry.objects.filter(pk=self.entry_id).update(notify_claimed_at=timezone.now() - timedelta(minutes=3))
        self.assertTrue(self.press().data['notify']['sent'])
        self.assertIsNone(self.entry().notify_claimed_at)

    @override_settings(**SENDING_ON)
    def test_the_row_is_marked_while_manychat_is_asked_and_the_mark_is_given_back(self):
        seen = []

        def manychat(method, path, **kwargs):
            seen.append(self.entry().notify_claimed_at)
            return self.manychat(method, path, **kwargs)

        with patch(REQUEST, side_effect=manychat):
            self.press()
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0])
        self.assertIsNone(self.entry().notify_claimed_at)

    @override_settings(**SENDING_ON)
    def test_a_seat_taken_and_freed_again_is_a_new_seat_to_tell_about(self):
        self.press()
        told_first = self.entry().notified_at
        self.fill()
        waiting_list.check()
        self.assertEqual(self.entry().status, 'waiting')
        self.assertEqual(self.entry().notified_at, told_first)                     # kept, as history
        self.assertIsNone(self.fresh().lead_waiting.get('notified_at'))
        self.free_a_seat()
        waiting_list.check()
        self.assertEqual(self.entry().status, 'freed')
        # The lead does not go by the telling of the seat that was lost…
        self.assertEqual(self.get(f'leads/{self.lead.id}/').data['next']['what'], 'להציע ללקוח את המקום שהתפנה')
        self.assertIsNone(self.get(f'contacts/{self.lead.id}/').data['waiting_list']['notified_at'])
        look = self.look().data['notify']
        self.assertEqual((look['needs_again'], look['already_notified_by_name']), (False, 'דנה מנהלת'))
        # …and the press needs no "again": it is another seat.
        response = self.press()
        self.assertEqual((response.status_code, response.data['notify']['sent']), (200, True))
        self.assertIsNotNone(response.data['notify']['already_notified_at'])

    @override_settings(**SENDING_ON)
    def test_two_wait_for_one_seat_and_the_office_tells_the_one_it_chose(self):
        other = Contact.objects.create(phone='972505550202', name='שני', manychat_subscriber_id='5151', last_inbound_at=timezone.now())
        self.fill()
        waiting_list.check()
        second = self.join(other).data['entry']['id']
        self.free_a_seat()
        waiting_list.check()
        self.assertEqual({row['id']: row['status'] for row in self.get('waiting-list/').data['results']}, {self.entry_id: 'freed', second: 'freed'})
        self.assertTrue(self.press(second).data['notify']['sent'])
        told = {row['id']: row['notified_at'] for row in self.get('waiting-list/').data['results']}
        self.assertIsNotNone(told[second])
        self.assertIsNone(told[self.entry_id])
        self.assertEqual((self.out(self.lead), len(self.out(other))), ([], 1))


class RefusedTests(NotifyCase):
    @override_settings(**SENDING_ON)
    def test_a_row_that_is_not_one_to_tell_about_sends_nothing(self):
        # The seat was taken again and the cron has not looked yet: the form's own count is read at the press.
        self.fill()
        response = self.press()
        self.assertEqual((response.status_code, response.data['code'], response.data['detail']), (409, 'taken', waiting_notify.DETAILS['taken']))
        self.assertEqual((response.data['entry']['id'], response.data['contact']['id']), (self.entry_id, self.lead.id))
        waiting_list.check()
        self.assertEqual(self.press().data['code'], 'not_freed')
        self.free_a_seat()
        waiting_list.check()
        self.young_lesson.status = 'cancelled'
        self.young_lesson.save(update_fields=['status'])
        self.assertEqual(self.press().data['code'], 'no_lesson')
        self.young_lesson.status = 'scheduled'
        self.young_lesson.save(update_fields=['status'])
        self.post(f'waiting-list/{self.entry_id}/handled/')
        response = self.press()
        self.assertEqual((response.status_code, response.data['code']), (409, 'not_active'))
        self.assertEqual(self.press(999999).status_code, 404)
        self.nothing_written()

    @override_settings(**SENDING_OFF)
    def test_what_is_wrong_with_the_row_is_said_before_what_is_wrong_with_the_pipe(self):
        self.fill()
        self.assertEqual(self.press().data['code'], 'taken')

    @override_settings(**SENDING_ON)
    def test_a_refusal_of_manychat_is_a_failed_line_with_the_reason_and_the_row_is_not_marked_told(self):
        self.manychat.refuse = ManyChatError('Validation error', status_code=400, payload={
            'status': 'error', 'message': 'Validation error', 'details': {'messages': [{'message': 'Subscriber is not subscribed to WhatsApp'}]},
        })
        notify = self.press().data['notify']
        self.assertEqual((notify['sent'], notify['how'], notify['code']), (False, 'text', 'failed'))
        self.assertTrue(notify['detail'].startswith('ManyChat דחה את הפעולה'))
        self.assertEqual((notify['message']['status'], notify['message']['error']), ('failed', notify['detail']))
        self.assertEqual(self.out(), [('text', 'failed', WORDS)])
        self.assertEqual((self.entry().notified_at, self.entry().notify_claimed_at), (None, None))
        self.assertEqual([line for line in self.journal() if TOLD in line], [])
        # It may be pressed again, with no "again": the customer was told nothing.
        self.manychat.refuse = None
        self.assertTrue(self.press().data['notify']['sent'])

    @override_settings(**SENDING_ON)
    def test_a_request_that_got_no_answer_says_so_and_is_not_repeated(self):
        self.door.stop()                                         # the client itself, down to the request that hangs
        with patch('apps.core.manychat_service.requests.request', side_effect=requests.ReadTimeout('timed out')) as http:
            notify = self.press().data['notify']
        self.assertEqual(http.call_count, 1)
        self.assertEqual((notify['sent'], notify['code']), (False, 'failed'))
        self.assertIn('לא ידוע אם ההודעה נשלחה', notify['detail'])
        self.assertEqual(self.out(), [('text', 'failed', WORDS)])
        # Nobody knows whether it arrived: the row is not marked told, and nothing tries again by itself.
        self.assertEqual((self.entry().notified_at, self.entry().notify_claimed_at), (None, None))

    def test_managers_only(self):
        others = {
            'partner': (client_for(make_user('partner@seat.test', UserProfile.ROLE_PARTNER)), 403),
            'worker': (client_for(make_user('worker@seat.test', UserProfile.ROLE_WORKER)), 403),
            'nobody': (APIClient(), 401),
        }
        for who, (client, refused) in others.items():
            with self.subTest(who=who):
                self.assertEqual(client.get(f'{BASE}/waiting-list/{self.entry_id}/notify/').status_code, refused)
                self.assertEqual(client.post(f'{BASE}/waiting-list/{self.entry_id}/notify/', {'again': True}, format='json').status_code, refused)
                self.assertEqual(client.get(f'{BASE}/waiting-list/lessons/').status_code, refused)
        self.nothing_written()


# --- the template on the settings card -------------------------------------------------------------------

class TemplateCardTests(NotifyCase):
    def row(self) -> dict:
        return next(row for row in lead_automation.templates() if row['template_key'] == 'seat_freed_1')

    def test_the_message_has_its_line_on_the_templates_card(self):
        row = self.row()
        self.assertEqual((row['kind'], row['variant'], row['step'], row['stage'], row['days']), ('seat_freed', '', 1, 2, None))
        self.assertEqual(row['kind_label'], 'הודעה ללקוח: התפנה מקום ברשימת ההמתנה')
        self.assertEqual(row['when_label'], 'בלחיצה של המשרד על "להודיע ללקוח", כשהתפנה מקום למי שברשימת ההמתנה')
        self.assertEqual((row['flow_name'], row['suggested_name']), ('kogo_lead_seat_1', 'kogo_lead_seat_1'))
        self.assertEqual((row['active'], row['required'], row['mapped'], row['by']), (True, True, False, None))
        self.assertEqual(row['text'], lead_followup_seed.DEFAULTS['lead_seat_freed']['texts'][0])
        self.assertEqual(waiting_notify.FLOW_NAME, lead_templates.FLOW_NAMES[waiting_notify.TEMPLATE_KEY])
        # It is the last of the list, and no part of what "ready" asks for: stage 1 is the five it always was.
        self.assertEqual(lead_automation.templates()[-1]['template_key'], 'seat_freed_1')
        self.assertEqual(lead_templates.STAGE_ONE, ('link_reminder_1', 'will_check_1', 'trial_feedback_1', 'trial_no_show_1', 'date_arrived_age_1'))

    @override_settings(MANYCHAT_KEY='test-key')
    def test_an_automation_under_its_name_connects_it_and_a_person_may_connect_another(self):
        self.manychat.named('seat_freed_1')
        row = self.row()
        self.assertEqual((row['mapped'], row['target'], row['by']), (True, SEAT_FLOW, 'name'))
        self.manychat.flows.append({'ns': 'content_other', 'name': 'מקום התפנה — נוסח אחר'})
        response = self.client.put(f'{BASE}/leads/templates/seat_freed_1/', {'target': 'content_other'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['mapped'], response.data['target'], response.data['by'], response.data['connected_by']), (True, 'content_other', 'manual', 'דנה מנהלת'))

    def test_the_engine_never_plans_it(self):
        """No round of the bot's has this kind: it is not one of the follow-ups the cron acts on."""
        from apps.wahub import lead_stage
        from apps.wahub.models import NUDGE_KIND_CHOICES, LeadFollowUp

        self.assertNotIn('seat_freed', dict(NUDGE_KIND_CHOICES))
        self.assertNotIn('seat_freed', lead_stage.NUDGE_DELAYS)
        self.assertEqual(lead_followups.phrasing('seat_freed', 1)[1], 'seat_freed_1')
        with override_settings(**SENDING_ON):
            self.press()
        self.assertFalse(LeadFollowUp.objects.filter(contact=self.lead).exists())
