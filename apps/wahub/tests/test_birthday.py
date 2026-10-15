"""
"מחכים לברכה ליום הולדת" (the owner, 15.10.2026), and the office's name on its messages:

- a customer's message that asks for Kogo's birthday greeting (or its video) tags the contact and starts the wait;
- the bot's tag "ברכת יום הולדת" is kept on the contact where its answer really left, and starts the wait too;
- the box "birthday" and its count, the open reason, and the box a search shows;
- ✓ ends the wait;
- a person with no name in the system is never shown by their email.

Nothing here sends anything: the network is cut (base.py), and the live answer is a stand-in.
"""
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from apps.wahub import birthday, live_test, queries, state
from apps.wahub.models import Contact, ContactEvent, KnowledgeItem, Message, ShadowReply, Tag
from apps.wahub.serializers import message_payload
from apps.wahub.tests.base import BASE, PHONE, WahubTestCase, client_for, make_user

ASKS = 'היי, אפשר לקבל ברכה ליום הולדת מקוגומלו לבן שלי? הוא חוגג 7 ביום חמישי'
OTHER = '972505550102'


def tag_call(tag, details=''):
    return {'name': 'tag_conversation', 'input': {'tag': tag, 'details': details}, 'summary': f'סומנה התגית "{tag}"'}


class BirthdayCase(WahubTestCase):
    def fresh(self, phone=PHONE) -> Contact:
        return Contact.objects.get(phone=phone)

    def tags(self, phone=PHONE) -> list:
        return sorted(self.fresh(phone).tags.values_list('name', flat=True))

    def journal(self, phone=PHONE) -> list:
        return list(ContactEvent.objects.filter(contact__phone=phone, kind='tags_changed').order_by('id').values_list('text', flat=True))

    def ids(self, box) -> list:
        return [row['id'] for row in self.get('contacts/', box=box).data['results']]


class AsksForTheGreetingTests(BirthdayCase):
    def test_the_ways_a_greeting_and_a_birthday_are_written(self):
        for text in (
            ASKS,
            'אפשר סרטון ליומולדת של הבת שלי?',
            'רציתי לבקש ברכת יום-הולדת לנועה',
            'שלום, ליום ההולדת שלו הוא מאוד רוצה ברכה מכם',
            'יש לכם ברכות ליום הולדת?',
            'אפשר סירטון יום הולדת לבן שלי?',
        ):
            with self.subTest(text=text):
                self.assertTrue(birthday.asks_for_greeting(text))

    def test_what_is_not_a_request_for_the_greeting(self):
        for text in (
            'אפשר לעשות אצלכם הפעלה ליום הולדת לבת 6?',       # a party — "הפעלת יום הולדת", another path
            'מה המחיר ליום הולדת לגיל 7?',
            'רוצים לחגוג יום הולדת אצלכם. בברכה, דנה',         # a letter's sign-off
            'תודה על הברכה!',                                  # no birthday in it
            '',
        ):
            with self.subTest(text=text):
                self.assertFalse(birthday.asks_for_greeting(text))

    def test_the_message_tags_the_contact_and_starts_the_wait_without_asking_for_a_person(self):
        before = timezone.now()
        self.incoming(ASKS)
        contact = self.fresh()
        self.assertIsNotNone(contact.birthday_wait_since)
        self.assertGreaterEqual(contact.birthday_wait_since, before)
        self.assertEqual(self.tags(), ['ברכת יום הולדת'])
        # The seed's colour for a tag the section did not have yet.
        self.assertEqual(Tag.objects.get(name='ברכת יום הולדת').color, '#db2777')
        self.assertFalse(contact.needs_human)
        [line] = self.journal()
        self.assertIn('נוספה התגית ברכת יום הולדת', line)
        self.assertIn('מחכה לברכה ליום הולדת', line)
        self.assertIn('ברכה ליום הולדת מקוגומלו', line)

    def test_a_second_message_keeps_the_first_moment_and_writes_nothing_twice(self):
        self.incoming(ASKS)
        first = self.fresh().birthday_wait_since
        self.bot_reply('בשמחה! איך קוראים לו?')
        self.incoming('יונתן. ושיהיה סרטון ליום הולדת אם אפשר')
        self.assertEqual(self.fresh().birthday_wait_since, first)
        self.assertEqual(self.tags(), ['ברכת יום הולדת'])
        self.assertEqual(len(self.journal()), 1)

    def test_an_existing_tag_of_that_name_is_the_one_used(self):
        mine = Tag.objects.create(name='ברכת יום הולדת', color='#000000')
        self.incoming(ASKS)
        self.assertEqual(list(self.fresh().tags.all()), [mine])
        self.assertEqual(Tag.objects.filter(name='ברכת יום הולדת').count(), 1)

    def test_the_bots_own_answer_with_the_words_starts_nothing(self):
        self.incoming('שלום')
        self.bot_reply('אפשר לבקש מאיתנו ברכה ליום הולדת בכל זמן 🙂')
        self.assertIsNone(self.fresh().birthday_wait_since)
        self.assertEqual(self.tags(), [])

    def test_a_thank_you_after_the_office_sent_the_greeting_does_not_open_it_again(self):
        self.incoming('שלום')
        contact = self.fresh()
        Message.objects.create(
            contact=contact, direction='out', sender='office', sender_name='מיכל', status='sent', source='kogo',
            text='היי, ברכת יום ההולדת שלך מחכה לך: https://example.test/v', sent_at=timezone.now() - timedelta(days=2),
        )
        self.incoming('תודה רבה על הברכה ליום הולדת! הוא היה בשמיים')
        self.assertIsNone(self.fresh().birthday_wait_since)
        # A greeting sent long ago is another year's: a request now is a request.
        Message.objects.filter(contact=contact, sender='office').update(sent_at=timezone.now() - timedelta(days=30))
        self.incoming(ASKS)
        self.assertIsNotNone(self.fresh().birthday_wait_since)


class TheBotsTagTests(BirthdayCase):
    def setUp(self):
        super().setUp()
        self.incoming('היי, הבן שלי חוגג יום הולדת ביום חמישי')
        self.lead = self.fresh()

    def test_the_greeting_tag_is_kept_with_its_details_and_starts_the_wait(self):
        added = birthday.apply_tag_marks(self.lead, [
            {'name': 'find_courses', 'input': {'city': 'ראש העין'}, 'summary': '…'},
            tag_call('ברכת יום הולדת', 'יונתן, בן 7, ראש העין'),
        ])
        self.assertEqual(added, 1)
        self.assertEqual(self.tags(), ['ברכת יום הולדת'])
        self.assertIsNotNone(self.fresh().birthday_wait_since)
        self.assertEqual(self.journal(), ['הבוט תייג: ברכת יום הולדת · מחכה לברכה ליום הולדת — יונתן, בן 7, ראש העין'])
        # The same answer twice makes nothing twice.
        self.assertEqual(birthday.apply_tag_marks(self.lead, [tag_call('ברכת יום הולדת')]), 0)
        self.assertEqual(len(self.journal()), 1)

    def test_another_tag_of_a_topic_is_kept_and_starts_no_wait(self):
        self.assertEqual(birthday.apply_tag_marks(self.lead, [tag_call('הפעלת יום הולדת', 'בת 6, 20 ילדים')]), 1)
        self.assertEqual(self.tags(), ['הפעלת יום הולדת'])
        self.assertEqual(Tag.objects.get(name='הפעלת יום הולדת').color, '#9333ea')
        self.assertIsNone(self.fresh().birthday_wait_since)

    def test_a_topic_of_the_knowledge_names_its_own_tag(self):
        KnowledgeItem.objects.create(kind='topic', title='סדנת קיץ', data={'tag': 'סדנת קיץ'})
        self.assertEqual(birthday.apply_tag_marks(self.lead, [tag_call(' סדנת  קיץ ')]), 1)
        self.assertEqual(self.tags(), ['סדנת קיץ'])

    def test_a_name_the_tool_refused_and_marks_that_are_not_marks_are_passed_over(self):
        for tools in (None, [], 'x', [None, {'name': 'tag_conversation'}, {'name': 'tag_conversation', 'input': 'x'}], [tag_call('לקוח VIP')]):
            with self.subTest(tools=tools):
                self.assertEqual(birthday.apply_tag_marks(self.lead, tools), 0)
        self.assertEqual(self.tags(), [])
        self.assertFalse(Tag.objects.filter(name='לקוח VIP').exists())
        self.assertIsNone(self.fresh().birthday_wait_since)
        self.assertEqual(birthday.apply_tag_marks(None, [tag_call('ברכת יום הולדת')]), 0)

    def test_it_never_raises_the_answer_has_already_left(self):
        with patch.object(birthday, '_tag_named', side_effect=RuntimeError('boom')):
            self.assertEqual(birthday.apply_tag_marks(self.lead, [tag_call('ברכת יום הולדת')]), 0)

    def _live_answer(self, *, delivered: bool):
        Contact.objects.filter(pk=self.lead.pk).update(live_test=True)

        def answer(contact, **_how):
            return ShadowReply.objects.create(
                contact=contact, text='בשמחה! העברתי לצוות, הברכה תישלח אליכם', tools_used=[tag_call('ברכת יום הולדת', 'יונתן')],
                after_message=Message.objects.filter(contact=contact, direction='in').order_by('-id').first(),
            )

        def send(contact, text):
            return Message.objects.create(
                contact=contact, direction='out', sender='bot', text=text, new_bot=True,
                status='sent' if delivered else 'failed', error='' if delivered else 'ManyChat דחה',
            )

        with patch.object(live_test.shadow, 'shadow_configured', return_value=True), \
                patch.object(live_test, '_settle', side_effect=lambda contact, deadline: contact), \
                patch.object(live_test.shadow, 'answer_now', side_effect=answer), \
                patch.object(live_test.sending, 'send_new_bot_reply', side_effect=send):
            return live_test.reply(self.lead.id)

    def test_a_live_answer_that_left_keeps_the_tag(self):
        self.assertEqual(self._live_answer(delivered=True)['outcome'], live_test.OUTCOME_SENT)
        self.assertEqual(self.tags(), ['ברכת יום הולדת'])
        self.assertIsNotNone(self.fresh().birthday_wait_since)

    def test_a_live_answer_that_did_not_reach_the_customer_keeps_none(self):
        self.assertEqual(self._live_answer(delivered=False)['outcome'], live_test.OUTCOME_FAILED)
        self.assertEqual(self.tags(), [])
        self.assertIsNone(self.fresh().birthday_wait_since)

    def test_the_new_bots_answer_in_a_demo_conversation_keeps_its_tag_there(self):
        demo = Contact.objects.create(phone='972505559001', name='דמו', is_demo=True, source='demo')

        def answer(contact, **_how):
            return ShadowReply.objects.create(contact=contact, text='בשמחה! הברכה בדרך', tools_used=[tag_call('ברכת יום הולדת')])

        with patch('apps.wahub.views.shadow.answer_now', side_effect=answer):
            response = self.client.post(f'{BASE}/demo/contacts/{demo.id}/reply/', {'post': True}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.tags(demo.phone), ['ברכת יום הולדת'])
        self.assertIsNotNone(self.fresh(demo.phone).birthday_wait_since)
        # Only answered — `post: false` writes nothing into the conversation, and keeps no tag.
        other = Contact.objects.create(phone='972505559002', name='דמו 2', is_demo=True, source='demo')
        with patch('apps.wahub.views.shadow.answer_now', side_effect=answer):
            self.client.post(f'{BASE}/demo/contacts/{other.id}/reply/', {'post': False}, format='json')
        self.assertEqual(self.tags(other.phone), [])


class TheBoxTests(BirthdayCase):
    def setUp(self):
        super().setUp()
        self.incoming(ASKS)
        self.bot_reply('בשמחה! העברתי לצוות')
        self.waits = self.fresh()
        # Somebody else, with two tags: a condition on the tags in the counts would count them twice.
        self.incoming('שלום, יש חוג קפוארה בראש העין?', phone=OTHER)
        other = self.fresh(OTHER)
        other.tags.set([Tag.objects.create(name='ראש העין'), Tag.objects.create(name='מודעה')])

    def test_the_conversation_is_open_for_a_person_and_says_why(self):
        chat = self.get(f'contacts/{self.waits.id}/').data['chat']
        self.assertEqual((chat['open'], chat['open_reason'], chat['open_label']), (True, 'birthday', 'מחכה לברכה ליום הולדת'))
        self.assertEqual((chat['box'], chat['box_label']), ('birthday', 'מחכה לברכה ליום הולדת'))
        self.assertEqual(chat['birthday_wait_since'], self.waits.birthday_wait_since.isoformat())
        other = self.get(f'contacts/{self.fresh(OTHER).id}/').data['chat']
        self.assertEqual((other['birthday_wait_since'], other['open']), (None, False))

    def test_the_box_lists_it_and_counts_it_once_and_every_other_count_is_untouched(self):
        self.assertEqual(self.ids('birthday'), [self.waits.id])
        self.assertIn(self.waits.id, self.ids('open'))
        counts = self.get('contacts/counts/').data['boxes']
        self.assertEqual((counts['birthday'], counts['open'], counts['all'], counts['bot']), (1, 1, 2, 2))
        self.assertEqual(self.get('contacts/updates/').data['boxes']['birthday'], 1)

    def test_somebody_who_also_asked_for_a_person_is_in_that_box_first(self):
        Contact.objects.filter(pk=self.waits.pk).update(needs_human=True)
        today = state.now_israel_date()
        waits = self.fresh()
        self.assertEqual(queries.box_of(waits, today)[0], 'needs_human')
        self.assertEqual(queries.open_reason(waits, today), 'needs_human')
        # Still counted where it waits.
        self.assertEqual(self.ids('birthday'), [waits.id])

    def test_closing_the_conversation_ends_the_wait(self):
        response = self.post(f'contacts/{self.waits.id}/close/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.data['chat']['open'], response.data['chat']['birthday_wait_since']), (False, None))
        self.assertIsNone(self.fresh().birthday_wait_since)
        # The tag stays: it says what the conversation was about.
        self.assertEqual(self.tags(), ['ברכת יום הולדת'])
        self.assertEqual((self.get('contacts/counts/').data['boxes']['birthday'], self.ids('birthday')), (0, []))


class NoEmailForANameTests(WahubTestCase):
    def test_a_person_with_no_name_is_nobody_by_name_not_their_email(self):
        nameless = make_user('office@wahub.test')
        self.assertEqual(state.user_display_name(nameless), '')
        self.assertEqual(state.user_display_name(self.manager), 'דנה מנהלת')
        self.assertEqual(state.user_display_name(make_user('first@wahub.test', first_name='מיכל')), 'מיכל')
        self.assertEqual(state.user_display_name(None), '')

    def test_an_office_message_kept_with_an_email_is_shown_with_no_name(self):
        self.incoming('שלום')
        contact = Contact.objects.get(phone=PHONE)
        old = Message.objects.create(contact=contact, direction='out', sender='office', sender_name='office@wahub.test', text='היי', status='sent', source='kogo')
        named = Message.objects.create(contact=contact, direction='out', sender='office', sender_name='מיכל', text='היי', status='sent', source='kogo')
        self.assertIsNone(message_payload(old)['sender_name'])
        self.assertEqual(message_payload(named)['sender_name'], 'מיכל')
        texts = {row['id']: row['sender_name'] for row in self.get(f'contacts/{contact.id}/messages/').data['messages']}
        self.assertEqual((texts[old.id], texts[named.id]), (None, 'מיכל'))

    def test_a_mark_by_a_person_with_no_name_says_no_email(self):
        self.incoming('שלום')
        contact = Contact.objects.get(phone=PHONE)
        nameless = client_for(make_user('office@wahub.test'))
        nameless.patch(f'{BASE}/contacts/{contact.id}/followup/', {'status': 'answered'}, format='json')
        data = nameless.get(f'{BASE}/contacts/{contact.id}/').data
        self.assertEqual(data['followup']['by_name'], '')
        self.assertNotIn('office@wahub.test', str(data))
