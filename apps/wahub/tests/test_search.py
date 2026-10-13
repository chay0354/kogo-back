"""
The one search of the section (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ח.1) and the
customer's card on every row (ח.2): a phone however it is typed and any part of
it, a name in any order and in part, the family the system knows behind a
contact, a search that crosses the boxes and the lists, and the customers that
have no conversation at all.

Every name and number here is invented.
"""
from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.core.models import UserProfile
from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.customer_details import SHARED_GHOST_FAMILY_NAME
from apps.wahub import search
from apps.wahub.models import Contact
from apps.wahub.tests.base import WahubTestCase, client_for, make_user


def names(response):
    return [row['name'] for row in response.data['results']]


class ReadingWhatWasTypedTests(WahubTestCase):
    def test_a_phone_is_one_number_however_it_is_typed(self):
        for typed in ('0501234567', '050-123-4567', '050 123 4567', '(050) 123.4567'):
            with self.subTest(typed=typed):
                self.assertEqual(search.parse(typed), [search.Phone('0501234567')])
        self.assertEqual(search.parse('+972 50 123 4567'), [search.Phone('972501234567')])
        self.assertEqual(search.parse('00972501234567'), [search.Phone('972501234567')])
        self.assertEqual(search.Phone('0501234567').in_contact, ('0501234567', '972501234567'))
        self.assertEqual(search.Phone('972501234567').in_customers, ('972501234567', '0501234567'))
        # A whole local number is also looked for as it is kept abroad-style and without its zero.
        self.assertEqual(search.Phone('0501234567').in_customers, ('0501234567', '972501234567', '501234567'))

    def test_four_digits_are_a_number_and_three_are_a_word(self):
        self.assertEqual(search.parse('1234'), [search.Phone('1234')])
        self.assertEqual(search.parse('050'), [search.Word('050', '050')])
        self.assertEqual(search.parse('  '), [])
        self.assertFalse(search.Search(''))
        self.assertFalse(search.Search(None))

    def test_words_and_a_number_together(self):
        self.assertEqual(
            search.parse('דנה 050 123 4567 לוי'),
            [search.Word('דנה', 'דנה'), search.Phone('0501234567'), search.Word('לוי', 'לוי')],
        )

    def test_a_name_is_compared_without_points_quotes_dashes_and_final_letters(self):
        self.assertEqual(search.fold('  גֶ\'ני   בן-דוד '), 'גני בנדוד')
        self.assertEqual(search.fold('ג׳ני'), 'גני')
        self.assertEqual(search.fold('ד"ר כהן'), 'דר כהנ')
        self.assertEqual(search.fold('Dana LEVI'), 'dana levi')
        self.assertEqual([word.folded for word in search.parse('בן-דוד')], ['בנ', 'דוד'])

    def test_no_more_than_six_words_and_two_numbers(self):
        tokens = search.parse('אא בב גג דד הה וו זז חח 0501111111 טט 0502222222 יי 0503333333')
        self.assertEqual(len([token for token in tokens if isinstance(token, search.Word)]), search.MAX_WORDS)
        self.assertEqual([token.digits for token in tokens if isinstance(token, search.Phone)], ['0501111111', '0502222222'])


class SearchCase(WahubTestCase):
    """
    One family the system knows with two WhatsApp contacts (the mother, the
    father), three contacts it does not know, and one family with no
    conversation at all.
    """

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        branch = TestDataFactory.create_branch()
        self.levi = TestDataFactory.create_family(name='לוי', branch=branch, phone='050-1234567')
        TestDataFactory.create_parent(family=self.levi, first_name='דנה', last_name='לוי', phone='050-1234567')
        TestDataFactory.create_parent(family=self.levi, first_name='יוסי', last_name='לוי', phone='052-7654321', is_primary=False)
        self.noam = TestDataFactory.create_child(family=self.levi, first_name='נועם', last_name='לוי', status='active')
        self.shira = TestDataFactory.create_child(
            family=self.levi, first_name='שירה', last_name='לוי', status='active', phone_number='054-1112222',
        )
        children = [str(self.noam.id), str(self.shira.id)]
        self.mother = self.make('972501234567', 'דנה לוי', 5, kogo_family_id=self.levi.id, kogo_child_ids=children, kogo_outcome='customer_before')
        self.father = self.make('972527654321', 'יוסי', 10, kogo_family_id=self.levi.id, kogo_child_ids=children, kogo_outcome='customer_before')
        self.jenny = self.make('972531110000', 'ג\'ני בן-דוד', 15)
        self.ronit = self.make('972549990000', 'רונית', 20, last_message_text='אשמח לדבר עם דנה מהמשרד')
        self.avi = self.make('972508887777', 'אבי כהן', 25, needs_human=True, needs_human_reason='ביקש נציג')

        # A customer nobody talks to on WhatsApp.
        self.sagi = TestDataFactory.create_family(name='שגיא', branch=branch, phone='058-4443322')
        TestDataFactory.create_parent(family=self.sagi, first_name='מירב', last_name='שגיא', phone='058-4443322')
        self.omer = TestDataFactory.create_child(family=self.sagi, first_name='עומר', last_name='שגיא', status='active')
        # The walk-ins' shared family, and a walk-in called like the mother: never anybody's customer.
        ghosts = TestDataFactory.create_family(name=SHARED_GHOST_FAMILY_NAME, branch=branch, phone='0000000000')
        TestDataFactory.create_child(family=ghosts, first_name='דנה', last_name='אורחת', status='ghost')

    def make(self, phone, name, minutes_ago, **fields):
        moment = self.now - timedelta(minutes=minutes_ago)
        defaults = dict(
            name=name, last_message_at=moment, last_inbound_at=moment, first_inbound_at=moment,
            last_message_direction='in', last_message_sender='customer', last_message_text='שלום',
        )
        defaults.update(fields)
        return Contact.objects.create(phone=phone, **defaults)

    def chats(self, term, **params):
        return self.get('contacts/', search=term, **{'box': 'all', **params})

    def found_by(self, response, contact):
        return next(row['found_by'] for row in response.data['results'] if row['id'] == contact.id)


class ConversationsSearchTests(SearchCase):
    def test_a_phone_in_every_spelling_and_any_part_of_it(self):
        for typed in ('0501234567', '050-123-4567', '972501234567', '+972 50 123 4567', '501234567', '0501234', '34567'):
            with self.subTest(typed=typed):
                response = self.chats(typed)
                # The mother by her own number, first; the father because it is his family's.
                self.assertEqual(names(response), ['דנה לוי', 'יוסי'])
                self.assertIsNone(self.found_by(response, self.mother))
                self.assertEqual(
                    self.found_by(response, self.father),
                    {'kind': 'family_phone', 'label': 'טלפון נוסף של המשפחה', 'value': '050-1234567'},
                )

    def test_three_digits_are_not_a_phone(self):
        self.assertEqual(names(self.chats('050')), [])

    def test_a_name_in_any_order_and_in_part(self):
        for typed in ('דנה לוי', 'לוי דנה', 'דנ לו', 'לו דנ', '  דנה    לוי '):
            with self.subTest(typed=typed):
                response = self.chats(typed)
                self.assertEqual(names(response), ['דנה לוי', 'יוסי'])
                self.assertEqual(self.found_by(response, self.father), {'kind': 'parent', 'label': 'הורה', 'value': 'דנה לוי'})

    def test_own_name_first_then_the_family_then_a_word_in_the_conversation(self):
        response = self.chats('דנה')
        self.assertEqual(names(response), ['דנה לוי', 'יוסי', 'רונית'])
        self.assertEqual(
            [row['found_by'] for row in response.data['results']],
            [None, {'kind': 'parent', 'label': 'הורה', 'value': 'דנה לוי'}, {'kind': 'text', 'label': 'מופיע בשיחה', 'value': ''}],
        )

    def test_by_a_child_of_the_family(self):
        response = self.chats('נועם')
        self.assertEqual(names(response), ['דנה לוי', 'יוסי'])
        said = {'kind': 'child', 'label': 'ילד/ה', 'value': 'נועם לוי'}
        self.assertEqual((self.found_by(response, self.mother), self.found_by(response, self.father)), (said, said))

    def test_by_the_other_phones_of_the_family(self):
        response = self.chats('052-7654321')
        self.assertEqual(names(response), ['יוסי', 'דנה לוי'])
        self.assertEqual(self.found_by(response, self.mother), {'kind': 'family_phone', 'label': 'טלפון נוסף של המשפחה', 'value': '052-7654321'})
        # A child's own number.
        response = self.chats('0541112222')
        self.assertEqual(set(names(response)), {'דנה לוי', 'יוסי'})
        self.assertEqual(self.found_by(response, self.father)['value'], '054-1112222')

    def test_points_quotes_dashes_and_final_letters_make_no_difference(self):
        for typed in ('גני', 'ג\'ני', 'ג׳ני', 'גֶ\'ני', 'בן דוד', 'בן-דוד', 'דוד גני'):
            with self.subTest(typed=typed):
                self.assertEqual(names(self.chats(typed)), ['ג\'ני בן-דוד'])
        for typed in ('כהן', 'כהנ', 'אבי כה'):
            with self.subTest(typed=typed):
                self.assertEqual(names(self.chats(typed)), ['אבי כהן'])

    def test_a_word_and_a_number_together(self):
        self.assertEqual(names(self.chats('דנה 0501234')), ['דנה לוי', 'יוסי'])
        self.assertEqual(names(self.chats('אבי 0501234')), [])

    def test_one_letter_does_not_bring_every_family(self):
        # The letter is looked for in the contact's own name and words. The father has none of it — and is
        # not brought in through his son נועם: by one letter, every family in the system would come back.
        self.assertEqual(set(names(self.chats('נ'))), {'דנה לוי', 'ג\'ני בן-דוד', 'רונית', 'אבי כהן'})

    def test_the_search_crosses_the_boxes_when_the_screen_asks_for_all(self):
        # Inside a box, as before: only who is in it.
        self.assertEqual(names(self.get('contacts/', search='אבי', box='needs_human')), ['אבי כהן'])
        self.assertEqual(names(self.get('contacts/', search='דנה', box='needs_human')), [])
        # With box=all every conversation is searched, and each row says where it stands.
        response = self.chats('דנה')
        self.assertEqual([(row['chat']['box'], row['chat']['box_label']) for row in response.data['results']], [('all', 'הבוט עונה')] * 3)
        row = self.chats('אבי').data['results'][0]
        self.assertEqual((row['chat']['box'], row['chat']['box_label']), ('needs_human', 'מבקש נציג'))

    def test_the_box_of_a_conversation_in_words(self):
        today = timezone.localtime(self.now).date()
        Contact.objects.filter(pk=self.jenny.pk).update(handled_by='human')
        Contact.objects.filter(pk=self.ronit.pk).update(waiting_since=self.now)
        Contact.objects.filter(pk=self.father.pk).update(
            followup_status='later', followup_due=today + timedelta(days=5), followup_at=self.now, handled_by='human',
        )
        Contact.objects.filter(pk=self.mother.pk).update(followup_status='later', followup_due=today, followup_at=self.now)
        rows = {row['id']: (row['chat']['box'], row['chat']['box_label']) for row in self.get('contacts/').data['results']}
        self.assertEqual(rows[self.jenny.id], ('human', 'בטיפול נציג'))
        self.assertEqual(rows[self.ronit.id], ('waiting', 'ממתין למענה'))
        self.assertEqual(rows[self.father.id], ('future', 'ליד עתידי'))
        self.assertEqual(rows[self.mother.id], ('open', 'ליד עתידי — הגיע היום'))
        self.assertEqual(rows[self.avi.id], ('needs_human', 'מבקש נציג'))

    def test_the_counts_of_the_boxes_are_of_what_was_found(self):
        boxes = self.get('contacts/counts/', search='דנה').data['boxes']
        self.assertEqual((boxes['all'], boxes['needs_human']), (3, 0))
        self.assertEqual(self.get('contacts/counts/', search='0508887777').data['boxes']['needs_human'], 1)

    def test_without_a_search_nothing_of_it_is_in_the_answer(self):
        response = self.get('contacts/')
        self.assertNotIn('customers', response.data)
        self.assertTrue(all('found_by' not in row for row in response.data['results']))

    def test_a_contact_the_matching_has_not_reached_is_found_by_itself_only(self):
        Contact.objects.filter(pk=self.father.pk).update(kogo_family_id=None, kogo_child_ids=[])
        self.assertEqual(names(self.chats('נועם')), ['דנה לוי'])
        self.assertEqual(names(self.chats('יוסי')), ['יוסי', 'דנה לוי'])

    def test_the_search_asks_the_same_number_of_questions_whatever_it_finds(self):
        def questions(term):
            with CaptureQueriesContext(connection) as seen:
                self.assertEqual(self.chats(term).status_code, 200)
            return len(seen)

        few = questions('דנה לוי')
        for index in range(30):
            self.make(f'9725066600{index:02d}', f'דנה לוי {index}', 30 + index, kogo_family_id=self.levi.id, kogo_child_ids=[str(self.noam.id)])
            self.make(f'9725077700{index:02d}', f'קרוב {index}', 60 + index, kogo_family_id=self.levi.id, kogo_child_ids=[str(self.noam.id)])
        self.assertEqual(questions('דנה לוי'), few)
        # The user and the profile; a query a word for its families; the count; the rows; their tags; the
        # people of the families (3); the customers and the contacts behind them.
        self.assertLessEqual(few, 14)

    def test_only_a_manager_searches(self):
        for role in (UserProfile.ROLE_WORKER, UserProfile.ROLE_PARTNER):
            with self.subTest(role=role):
                client = client_for(make_user(f'{role}@wahub.test', role=role))
                for path in ('contacts/', 'leads/', 'waiting-list/'):
                    response = client.get(f'/api/v1/wahub/{path}', {'search': '0584443322', 'q': '0584443322'})
                    self.assertEqual(response.status_code, 403)
                    self.assertNotIn('customers', response.data)


class CustomersWithNoConversationTests(SearchCase):
    def test_a_number_that_is_a_customer_and_not_a_contact(self):
        for typed in ('0584443322', '058-444-3322', '972584443322', '4443322'):
            with self.subTest(typed=typed):
                response = self.chats(typed)
                self.assertEqual(response.data['results'], [])
                self.assertEqual(response.data['customers_more'], False)
                self.assertEqual(response.data['customers'], [{
                    'family_id': str(self.sagi.id), 'name': 'מירב שגיא', 'family_name': 'שגיא',
                    'phone': '058-4443322', 'phone_display': '058-4443322',
                    'children': [{'id': str(self.omer.id), 'name': 'עומר שגיא', 'status': 'active', 'status_label': 'פעיל'}],
                    'child_id': str(self.omer.id), 'link': f'/customers?child={self.omer.id}',
                    'contact_id': None,
                    'found_by': {'kind': 'phone', 'label': 'טלפון', 'value': '058-4443322'},
                }])

    def test_by_the_names_of_the_family(self):
        self.assertEqual(self.chats('עומר').data['customers'][0]['found_by'], {'kind': 'child', 'label': 'ילד/ה', 'value': 'עומר שגיא'})
        self.assertEqual([row['family_name'] for row in self.chats('שגיא מירב').data['customers']], ['שגיא'])
        self.assertEqual(self.chats('מירב').data['customers'][0]['found_by'], {'kind': 'parent', 'label': 'הורה', 'value': 'מירב שגיא'})

    def test_a_family_with_a_conversation_is_not_offered_and_neither_are_walk_ins(self):
        response = self.chats('דנה')
        # The Levis are behind two contacts; the walk-in called דנה is nobody's customer.
        self.assertEqual(response.data['customers'], [])
        self.assertEqual(self.chats('לוי').data['customers'], [])

    def test_nothing_anywhere(self):
        response = self.chats('0599999999')
        self.assertEqual((response.data['results'], response.data['customers'], response.data['customers_more']), ([], [], False))

    def test_a_family_with_no_child_has_no_card_to_open(self):
        lone = TestDataFactory.create_family(name='בודדת', phone='050-9090909')
        TestDataFactory.create_parent(family=lone, first_name='טל', last_name='בודדת', phone='050-9090909')
        row = self.chats('0509090909').data['customers'][0]
        self.assertEqual((row['children'], row['child_id'], row['link']), ([], None, None))

    def test_a_contact_with_that_phone_that_was_not_matched_yet(self):
        waiting = self.make('972584443322', 'מירב', 3)
        response = self.chats('0584443322')
        self.assertEqual(names(response), ['מירב'])
        self.assertEqual(response.data['customers'][0]['contact_id'], waiting.id)
        # Once the matching put the family on the contact, the family is no longer "a customer with no conversation".
        Contact.objects.filter(pk=waiting.pk).update(kogo_family_id=self.sagi.id)
        self.assertEqual(self.chats('0584443322').data['customers'], [])

    def test_no_more_than_five_and_only_on_the_first_page(self):
        for index in range(7):
            family = TestDataFactory.create_family(name=f'שגיא {index}', phone=f'05811100{index:02d}')
            TestDataFactory.create_child(family=family, first_name='רז', last_name='שגיא', status='active')
        response = self.chats('שגיא')
        self.assertEqual((len(response.data['customers']), response.data['customers_more']), (5, True))
        for index in range(3):
            self.make(f'9725012300{index:02d}', f'שגיא בשיחה {index}', index)
        second = self.chats('שגיא', page_size=2, page=2)
        self.assertEqual(second.status_code, 200)
        self.assertNotIn('customers', second.data)


class CustomerCardTests(SearchCase):
    def test_every_contact_says_where_its_card_is(self):
        rows = {row['id']: row['customer'] for row in self.get('contacts/').data['results']}
        self.assertEqual(rows[self.mother.id], {
            'family_id': str(self.levi.id), 'child_id': str(self.noam.id), 'link': f'/customers?child={self.noam.id}',
        })
        self.assertIsNone(rows[self.jenny.id])
        self.assertEqual(self.get(f'contacts/{self.mother.id}/').data['customer']['child_id'], str(self.noam.id))

    def test_a_family_with_no_child_has_no_link(self):
        Contact.objects.filter(pk=self.jenny.pk).update(kogo_family_id=self.sagi.id, kogo_child_ids=[])
        self.assertEqual(
            self.get(f'contacts/{self.jenny.id}/').data['customer'], {'family_id': str(self.sagi.id), 'child_id': None, 'link': None},
        )

    def test_a_lead_says_it_too(self):
        Contact.objects.filter(pk=self.mother.pk).update(lead_stage='in_conversation', lead_owner='')
        row = self.get('leads/', owner='bot').data['results'][0]
        self.assertEqual(row['customer'], {
            'family_id': str(self.levi.id), 'child_id': str(self.noam.id), 'link': f'/customers?child={self.noam.id}',
        })
        self.assertEqual(self.get(f'leads/{self.mother.id}/').data['customer'], row['customer'])


class LeadsSearchTests(SearchCase):
    def setUp(self):
        super().setUp()
        stamp = self.now - timedelta(hours=1)
        Contact.objects.filter(pk=self.mother.pk).update(lead_stage='needs_reply', lead_owner='office', lead_stage_since=stamp)
        Contact.objects.filter(pk=self.father.pk).update(lead_stage='in_conversation', lead_owner='', lead_stage_since=stamp)
        Contact.objects.filter(pk=self.avi.pk).update(lead_stage='not_interested', lead_owner='', lead_stage_since=stamp, lead_reason='אמר שלא מתאים לנועם')
        # ג'ני and רונית are conversations, not leads.

    def test_every_list_at_once_and_each_row_says_which(self):
        response = self.get('leads/', owner='all', q='לוי')
        self.assertEqual(response.status_code, 200)
        rows = [(row['name'], row['list'], row['list_label']) for row in response.data['results']]
        # By her own name first; her husband through the family's.
        self.assertEqual(rows, [('דנה לוי', 'office', 'מחכים לך'), ('יוסי', 'bot', 'הבוט מטפל')])
        self.assertEqual(response.data['found'], {'office': 1, 'bot': 1, 'waiting': 0, 'closed': 0, 'total': 2})
        self.assertEqual(
            [row['found_by'] for row in response.data['results']], [None, {'kind': 'family', 'label': 'שם המשפחה', 'value': 'לוי'}],
        )

    def test_the_family_and_the_leads_own_sentence(self):
        response = self.get('leads/', owner='all', search='נועם')
        rows = [(row['name'], row['list'], row['found_by']['kind']) for row in response.data['results']]
        # Through the family first (the two parents of נועם, either order), then the lead whose sentence names him.
        self.assertEqual(sorted(rows[:2]), [('דנה לוי', 'office', 'child'), ('יוסי', 'bot', 'child')])
        self.assertEqual(rows[2], ('אבי כהן', 'closed', 'text'))
        self.assertEqual(response.data['found'], {'office': 1, 'bot': 1, 'waiting': 0, 'closed': 1, 'total': 3})

    def test_inside_one_list_as_before(self):
        response = self.get('leads/', owner='bot', q='0501234567')
        self.assertEqual([row['name'] for row in response.data['results']], ['יוסי'])
        # What was found on every list is said all the same.
        self.assertEqual(response.data['found']['total'], 2)

    def test_who_has_the_name_without_being_a_lead(self):
        response = self.get('leads/', owner='all', q='גני')
        self.assertEqual(response.data['results'], [])
        self.assertEqual(response.data['contacts'], [{
            'id': self.jenny.id, 'name': 'ג\'ני בן-דוד', 'phone_display': '053-1110000', 'is_demo': False,
            'box': 'all', 'box_label': 'הבוט עונה', 'found_by': None, 'customer': None,
            'link': f'/wahub?tab=chats&contact={self.jenny.id}',
        }])
        # A word that is only in somebody's last message is no reason to offer their conversation.
        self.assertEqual(self.get('leads/', owner='all', q='מהמשרד').data['contacts'], [])

    def test_a_customer_with_no_conversation(self):
        response = self.get('leads/', owner='all', q='0584443322')
        self.assertEqual((response.data['results'], response.data['contacts']), ([], []))
        self.assertEqual([row['family_id'] for row in response.data['customers']], [str(self.sagi.id)])

    def test_without_a_search_the_answer_is_as_it_was(self):
        response = self.get('leads/', owner='all')
        self.assertEqual(len(response.data['results']), 3)
        for key in ('found', 'contacts', 'customers', 'customers_more'):
            self.assertNotIn(key, response.data)
        self.assertTrue(all('found_by' not in row for row in response.data['results']))

    def test_an_owner_nobody_knows(self):
        self.assertEqual(self.get('leads/', owner='everyone').status_code, 400)
