"""
"מחכים שיתפנה מקום" (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ח.3): the leads of the
waiting list under a heading of their own — whoever asks for the office first —
and the summary a person reads before picking up the phone: what they wanted,
for whom, since when, how full the lesson is now, and what can be offered
instead this minute.

Who answers the customer is not what changed: a lead whose seat came free is
still the office's. Every name and number here is invented.
"""
from datetime import time, timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.core.tests.test_fixtures import TestDataFactory
from apps.enrollments.models import LessonEnrollment
from apps.wahub import state, waiting_list, waiting_summary
from apps.wahub.models import Contact, WaitingListEntry
from apps.wahub.tests.base import PHONE
from apps.wahub.tests.test_waiting_list import LABEL, WaitingListCase

WAITS, FREED, TOLD, ANSWERED = '972505550201', PHONE, '972505550203', '972505550204'
TITLE = 'מחכים שיתפנה מקום'


class HeadingCase(WaitingListCase):
    """
    Four people on waiting lists: one whose lesson is still full, and three
    whose seat came free — one nobody told yet, one who was told, one who was
    told and answered.
    """

    def setUp(self):
        super().setUp()
        for phone in (WAITS, TOLD, ANSWERED):
            self.incoming('אפשר רשימת המתנה?', phone=phone)
            self.bot_reply('בודקת', phone=phone)
        self.fill()                                    # Tuesday 17:00, one seat
        self.fill(self.older_sunday)                   # Sunday 16:00, one seat — stays full
        self.assertEqual(self.join(child='בן 5').status_code, 201)
        self.assertEqual(self.join(self.contact(TOLD)).status_code, 201)
        self.assertEqual(self.join(self.contact(ANSWERED)).status_code, 201)
        self.assertEqual(self.join(self.contact(WAITS), self.older_sunday).status_code, 201)
        # The seat of Tuesday comes free: all three who wait for it are "freed".
        row = next(row for row in self.sitting if row.lesson_id == self.young_lesson.id)
        row.status = 'cancelled'
        row.save(update_fields=['status'])
        self.assertEqual(waiting_list.check()['waiting_freed'], 3)
        self.tell(TOLD)
        self.tell(ANSWERED)
        self.incoming('כן, רוצים את המקום', phone=ANSWERED)

    def tell(self, phone):
        """What waiting_notify writes when a message really left, without sending one."""
        entry = WaitingListEntry.objects.get(contact__phone=phone, lesson_id=self.young_lesson.id)
        entry.notified_at, entry.notified_by, entry.notified_how = timezone.now(), self.manager, 'text'
        entry.save(update_fields=['notified_at', 'notified_by', 'notified_how'])
        waiting_list.sync_contact(entry.contact_id)

    def leads(self, **params):
        response = self.get('leads/', **params)
        self.assertEqual(response.status_code, 200)
        return response.data

    def phones(self, **params):
        return [row['phone'] for row in self.leads(**params)['results']]


class TheListOfItsOwnTests(HeadingCase):
    def test_whoever_asks_for_the_office_is_first(self):
        data = self.leads(owner='waiting', waiting='apart')
        self.assertEqual([row['phone'] for row in data['results']], [ANSWERED, FREED, TOLD, WAITS])
        self.assertEqual(
            [(row['waiting_list']['state'], row['waiting_list']['state_label'], row['waiting_list']['needs_office']) for row in data['results']],
            [('answered', 'הלקוח ענה', True), ('freed', 'התפנה מקום', True), ('told', 'הודענו ללקוח', False), ('waiting', 'ממתין', False)],
        )
        self.assertTrue(all((row['list'], row['list_label'], row['stage']) == ('waiting', TITLE, 'waiting_list') for row in data['results']))
        self.assertEqual(data['stages'], [{'key': 'waiting_list', 'label': 'ברשימת המתנה', 'count': 4}])
        # Who answers the customer did not change: a seat that came free is the office's to offer.
        self.assertEqual([row['owner'] for row in data['results']], ['office', 'office', 'office', ''])

    def test_the_place_in_the_queue_of_each(self):
        rows = {row['phone']: row['waiting_list'] for row in self.leads(owner='waiting')['results']}
        self.assertEqual((rows[FREED]['position'], rows[FREED]['queue']), (1, 3))
        self.assertEqual((rows[TOLD]['position'], rows[ANSWERED]['position']), (2, 3))
        self.assertEqual((rows[WAITS]['position'], rows[WAITS]['queue']), (1, 1))

    def test_the_counts_of_the_heading(self):
        apart = self.leads(owner='office', waiting='apart')['counts']
        self.assertEqual(apart, {'office': 0, 'bot': 0, 'closed': 0, 'waiting': 4, 'waiting_act': 2})
        # A screen that does not know the heading still counts them where they were.
        legacy = self.leads(owner='office')['counts']
        self.assertEqual(legacy, {'office': 3, 'bot': 1, 'closed': 0, 'waiting': 4, 'waiting_act': 2})

    def test_apart_takes_them_out_of_the_two_other_lists(self):
        self.assertEqual(self.phones(owner='office', waiting='apart'), [])
        self.assertEqual(self.phones(owner='bot', waiting='apart'), [])
        self.assertEqual(self.leads(owner='bot', waiting='apart')['stages'], [])

    def test_a_screen_that_does_not_ask_for_it_sees_what_it_saw(self):
        self.assertEqual(set(self.phones(owner='office')), {FREED, TOLD, ANSWERED})
        self.assertEqual(self.phones(owner='bot'), [WAITS])
        # ... and every row says which of the four lists it belongs under all the same.
        self.assertEqual({row['list'] for row in self.leads(owner='office')['results']}, {'waiting'})

    def test_a_lead_of_another_stage_keeps_its_list(self):
        self.incoming('שלום, יש חוג ריקוד?', phone='972505550299')
        other = self.contact('972505550299')
        Contact.objects.filter(pk=other.pk).update(lead_stage='needs_reply', lead_owner='office')
        self.assertEqual(self.phones(owner='office', waiting='apart'), ['972505550299'])
        row = self.leads(owner='office', waiting='apart')['results'][0]
        self.assertEqual((row['list'], row['list_label'], row['waiting_list']), ('office', 'מחכים לך', None))
        self.assertEqual(self.leads(owner='office', waiting='apart')['counts']['office'], 1)

    def test_every_list_at_once(self):
        self.assertEqual(len(self.leads(owner='all')['results']), 4)

    def test_today_counts_them_under_their_own_name(self):
        summary = self.get('summary/').data
        self.assertEqual(summary['waiting_list'], {'label': TITLE, 'total': 4, 'act': 2})
        # "דורש טיפול" is as it was: the waiting list was never one of its stages.
        self.assertNotIn('waiting_list', [stage['key'] for stage in summary['lead_act']['stages']])
        with self.assertNumQueries(5):          # the user, the profile, and the three of the summary
            self.get('summary/')

    def test_the_conversation_says_where_the_row_stands(self):
        block = self.get(f'contacts/{self.contact(ANSWERED).id}/').data['waiting_list']
        self.assertEqual((block['state'], block['state_label'], block['needs_office']), ('answered', 'הלקוח ענה', True))
        self.assertEqual((block['position'], block['queue']), (None, None))
        block = self.get(f'contacts/{self.contact(WAITS).id}/').data['waiting_list']
        self.assertEqual((block['state'], block['needs_office']), ('waiting', False))

    def test_a_seat_taken_again_goes_back_to_the_end(self):
        _family, child = self.family_with_child(phone='0507770099')
        LessonEnrollment.objects.create(child=child, lesson=self.young_lesson, status='active')
        waiting_list.check()
        data = self.leads(owner='waiting', waiting='apart')
        self.assertEqual({row['waiting_list']['state'] for row in data['results']}, {'waiting'})
        self.assertEqual((data['counts']['waiting'], data['counts']['waiting_act']), (4, 0))

    def test_an_odd_value_in_the_copy_does_not_break_the_lists(self):
        contact = self.contact(TOLD)
        Contact.objects.filter(pk=contact.pk).update(lead_waiting={**contact.lead_waiting, 'notified_at': 'אתמול'})
        data = self.leads(owner='waiting')
        self.assertEqual(len(data['results']), 4)
        # Read as "not told": the seat is still to be offered.
        self.assertEqual(data['counts']['waiting_act'], 3)

    def test_the_list_does_not_grow_queries_with_rows(self):
        def questions():
            with CaptureQueriesContext(connection) as seen:
                self.leads(owner='waiting', waiting='apart')
            return len(seen)

        before = questions()
        for index in range(6):
            phone = f'9725055503{index:02d}'
            self.incoming('רשימת המתנה בבקשה', phone=phone)
            self.assertEqual(self.join(self.contact(phone), self.older_sunday).status_code, 201)
        self.assertEqual(len(self.leads(owner='waiting')['results']), 10)
        self.assertEqual(questions(), before)


class SearchingTheWaitingListTests(HeadingCase):
    def setUp(self):
        super().setUp()
        Contact.objects.filter(phone=FREED).update(name='רותם אלון')
        Contact.objects.filter(phone=WAITS).update(name='גלית שחר')

    def rows(self, **params):
        response = self.get('waiting-list/', **params)
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_by_the_contact(self):
        data = self.rows(search='רותם')
        self.assertEqual([(row['name'], row['found_by']) for row in data['results']], [('רותם אלון', None)])
        self.assertEqual(data['found'], 1)
        # The counts are of the whole list; the lessons are of what was found.
        self.assertEqual(data['counts'], {'waiting': 1, 'freed': 3})
        self.assertEqual([lesson['id'] for lesson in data['lessons']], [str(self.young_lesson.id)])
        self.assertEqual([row['name'] for row in self.rows(q='0505550201')['results']], ['גלית שחר'])

    def test_by_the_lesson_itself(self):
        data = self.rows(search='ואקרובטיקה')
        self.assertEqual([row['name'] for row in data['results']], ['גלית שחר'])
        self.assertEqual(data['results'][0]['found_by'], {
            'kind': 'lesson', 'label': 'השיעור', 'value': 'ג-ד קפוארה ואקרובטיקה · יום ראשון · 16:00 · קרל וגרטי קורי 8',
        })
        self.assertEqual(len(self.rows(search='יום שלישי')['results']), 3)
        self.assertEqual(len(self.rows(search='ראשון 16:00')['results']), 1)
        self.assertEqual(len(self.rows(search='בן 5')['results']), 1)
        self.assertEqual(self.rows(search='שחייה')['results'], [])

    def test_with_the_other_filters(self):
        self.assertEqual(len(self.rows(search='קפוארה', status='freed')['results']), 3)
        self.assertEqual(len(self.rows(search='קפוארה', status='waiting')['results']), 1)
        self.assertEqual(self.rows(search='רותם', lesson=str(self.older_sunday.id))['results'], [])

    def test_who_has_the_name_and_is_not_on_the_list(self):
        self.incoming('שלום', phone='972505550288', name='רותם בצר')
        outsider = self.contact('972505550288')
        data = self.rows(search='רותם')
        self.assertEqual([row['name'] for row in data['results']], ['רותם אלון'])
        self.assertEqual([(row['id'], row['name'], row['link']) for row in data['contacts']], [
            (outsider.id, 'רותם בצר', f'/wahub?tab=chats&contact={outsider.id}'),
        ])
        self.assertEqual((data['customers'], data['customers_more']), ([], False))

    def test_without_a_search_the_answer_is_as_it_was(self):
        data = self.rows()
        self.assertEqual(set(data), {'counts', 'lessons', 'results'})
        self.assertTrue(all('found_by' not in row for row in data['results']))


class SummaryCase(WaitingListCase):
    """The branch of the fixture, with what could stand in for Tuesday 17:00 of the group for ages 4.5–6."""

    def setUp(self):
        super().setUp()
        self.fill()
        self.assertEqual(self.join(child='בן 5').status_code, 201)
        self.entry = WaitingListEntry.objects.get()
        self.since = timezone.localtime(self.entry.created_at).date()

    def lesson(self, course, day, start, **extra):
        return TestDataFactory.create_lesson(
            course=course, room=self.room, instructor=self.moshe, day_of_week=day,
            start_time=time(start, 0), end_time=time(start, 45), **extra,
        )

    def course(self, name, *, branch=None, course_type=None, min_age=2, max_age=2, **extra):
        return TestDataFactory.create_course(
            name=name, branch=branch or self.branch, course_type=course_type or self.capoeira, price=Decimal('260.00'),
            min_age=min_age, max_age=max_age, capacity=12, **extra,
        )

    def stand_ins(self):
        """Everything that could be offered instead — and everything that must not be."""
        self.thursday = self.lesson(self.young, 4, 17)                                   # another hour of the same class
        self.second_group = self.lesson(self.course('קפוארה 4.5-6 קבוצה ב'), 2, 16, capacity=8)
        self.wide_group = self.lesson(self.course('קפוארה 3-6', min_age=1), 1, 17)       # ages overlap, not the very range
        other_branch = TestDataFactory.create_branch(name='הזמיר', city=self.branch.city)
        self.same_city = self.lesson(self.course('קפוארה גן חובה הזמיר', branch=other_branch), 3, 17)
        # Not to be offered: a full one, another field, another age group, another city, an inactive
        # class, a cancelled lesson, a class of an outside branch.
        full = self.lesson(self.course('קפוארה 4.5-6 מלאה'), 2, 18, capacity=1)
        _family, child = self.family_with_child(phone='0507770077')
        LessonEnrollment.objects.create(child=child, lesson=full, status='active')
        self.lesson(self.course('ריקוד 4.5-6', course_type=self.dance), 2, 17)
        far = TestDataFactory.create_branch(name='רחוק', city=TestDataFactory.create_city('חיפה'))
        self.lesson(self.course('קפוארה 4.5-6 חיפה', branch=far), 2, 17)
        self.lesson(self.course('קפוארה 4.5-6 סגורה', is_active=False), 2, 17)
        self.lesson(self.course('קפוארה 4.5-6 בוטל'), 2, 17, status='cancelled')

    def summary(self, contact=None):
        response = self.get(f'leads/{(contact or self.lead).id}/')
        self.assertEqual(response.status_code, 200)
        return response.data['details']['waiting_summary']


class SummaryTests(SummaryCase):
    def test_what_they_wanted_in_one_sentence(self):
        summary = self.summary()
        day = f'{self.since.day}.{self.since.month}'
        self.assertEqual(
            summary['sentence'],
            f'רצו *קפוארה 4.5-6 יום שלישי* ביום שלישי 17:00 בקרל וגרטי קורי 8, לבן 5 · ממתינים מ-{day}, ראשונים בתור',
        )
        self.assertEqual(summary['sentence_plain'], summary['sentence'].replace('*', ''))
        self.assertEqual(summary['wanted'], {
            'lesson_id': str(self.young_lesson.id), 'course_id': str(self.young.id), 'course': 'קפוארה 4.5-6 יום שלישי',
            'day': 'שלישי', 'start': '17:00', 'end': '17:45', 'branch': 'קרל וגרטי קורי 8', 'city': 'ראש העין',
            'instructor': 'משה לוי', 'ages': 'גיל 4.5-6', 'label': LABEL, 'exists': True,
        })
        self.assertEqual((summary['child'], summary['since_label'], summary['days']), ('בן 5', day, 0))
        self.assertEqual((summary['position'], summary['queue'], summary['position_label']), (1, 1, 'היחידים בתור'))
        self.assertEqual(summary['seats'], {
            'enrolled': 1, 'capacity': 1, 'seats_left': 0, 'is_full': True, 'seats_label': '1 מתוך 1 · מלא',
        })
        self.assertEqual((summary['state'], summary['state_label'], summary['needs_office']), ('waiting', 'ממתין', False))
        self.assertEqual((summary['entry_id'], summary['notified_at'], summary['customer_replied_at']), (self.entry.id, None, None))

    def test_the_number_to_call(self):
        summary = self.summary()
        self.assertEqual((summary['phone'], summary['phone_display'], summary['tel']), (PHONE, '050-5550101', f'tel:+{PHONE}'))
        self.assertIsNone(summary['customer'])

    def test_nothing_to_offer_is_said_in_words(self):
        summary = self.summary()
        self.assertEqual(summary['alternatives'], [])
        self.assertEqual(summary['alternatives_note'], 'אין כרגע מועד אחר עם מקום — לא באותו חוג ולא בחוג דומה באותה עיר')

    def test_what_can_be_offered_instead_the_nearest_first(self):
        self.stand_ins()
        summary = self.summary()
        self.assertEqual(summary['alternatives_note'], '')
        self.assertEqual(
            [(row['kind'], row['course'], row['day'], row['start'], row['seats_label']) for row in summary['alternatives']],
            [
                # The room of the fixture holds ten: the tightest of the lesson, the group and the room.
                ('same_course', 'קפוארה 4.5-6 יום שלישי', 'חמישי', '17:00', 'נשארו 10'),
                ('same_branch', 'קפוארה 4.5-6 קבוצה ב', 'שלישי', '16:00', 'נשארו 8'),
                ('same_branch', 'קפוארה 3-6', 'שני', '17:00', 'נשארו 10'),
                ('same_city', 'קפוארה גן חובה הזמיר', 'רביעי', '17:00', 'נשארו 10'),
            ],
        )
        first = summary['alternatives'][0]
        self.assertEqual(first, {
            'lesson_id': str(self.thursday.id), 'course_id': str(self.young.id), 'course': 'קפוארה 4.5-6 יום שלישי',
            'day': 'חמישי', 'start': '17:00', 'end': '17:45', 'branch': 'קרל וגרטי קורי 8', 'city': 'ראש העין',
            'instructor': 'משה לוי', 'ages': 'גיל 4.5-6',
            'label': 'קפוארה 4.5-6 יום שלישי · יום חמישי · 17:00 · קרל וגרטי קורי 8',
            'kind': 'same_course', 'kind_label': 'מועד אחר של אותו חוג',
            'enrolled': 0, 'capacity': 10, 'seats_left': 10, 'seats_label': 'נשארו 10',
        })
        self.assertEqual(
            [row['kind_label'] for row in summary['alternatives'][1::2]], ['חוג אחר באותו סניף', 'סניף אחר באותה עיר'],
        )

    def test_one_seat_left_and_a_lesson_with_no_limit(self):
        self.stand_ins()
        _family, child = self.family_with_child(phone='0507770066')
        for index in range(7):
            LessonEnrollment.objects.create(
                child=TestDataFactory.create_child(family=child.family, first_name=f'ילד {index}', status='active'),
                lesson=self.second_group, status='active',
            )
        labels = {row['course']: (row['enrolled'], row['seats_left'], row['seats_label']) for row in self.summary()['alternatives']}
        self.assertEqual(labels['קפוארה 4.5-6 קבוצה ב'], (7, 1, 'נשאר מקום אחד'))
        # A lesson nobody set a limit for has room, and says so without a number.
        self.assertEqual(
            waiting_summary._room({'capacity': None, 'available_spots': None, 'enrolled_count': 4, 'is_full': False}), 'יש מקום · בלי מגבלה',
        )

    def test_no_more_than_six(self):
        self.stand_ins()
        for day in (0, 1, 3, 5):
            self.lesson(self.young, day, 17)
        self.assertEqual(len(self.summary()['alternatives']), waiting_summary.ALTERNATIVES_SHOWN)
        self.assertEqual({row['kind'] for row in self.summary()['alternatives'][:5]}, {'same_course'})

    def test_a_lesson_they_already_wait_for_is_not_an_offer(self):
        self.thursday = self.lesson(self.young, 4, 17, capacity=1)
        _family, child = self.family_with_child(phone='0507770055')
        LessonEnrollment.objects.create(child=child, lesson=self.thursday, status='active')
        self.assertEqual(self.join(lesson=self.thursday).status_code, 201)
        # Thursday comes free: it is the row that leads now, and Tuesday — still waited for — is no stand-in for it.
        LessonEnrollment.objects.filter(lesson=self.thursday).update(status='cancelled')
        waiting_list.check()
        summary = self.summary()
        self.assertEqual((summary['wanted']['day'], summary['state'], summary['needs_office']), ('חמישי', 'freed', True))
        self.assertEqual(summary['alternatives'], [])
        self.assertEqual([(row['wanted']['day'], row['state']) for row in summary['others']], [('שלישי', 'waiting')])
        self.assertEqual([row['alternatives'] for row in summary['others']], [[]])
        self.assertNotIn('phone', summary['others'][0])

    def test_the_child_narrows_the_age_group(self):
        wide = self.course('קפוארה א-ג', min_age=3, max_age=5)
        codes = waiting_summary._child_codes
        self.assertEqual(codes('כיתה ב', wide), {4})
        self.assertEqual(codes('בת 8', wide), {4, 5})
        self.assertEqual(codes('נועם', wide), {3, 4, 5})
        self.assertEqual(codes('בן 15', wide), {3, 4, 5})          # outside the class's range: the range itself
        self.assertEqual(codes('בן 5', self.course('בלי טווח', min_age=None, max_age=None)), set())

    def test_two_in_the_queue(self):
        self.incoming('גם אנחנו', phone=WAITS)
        self.assertEqual(self.join(self.contact(WAITS)).status_code, 201)
        second = self.summary(self.contact(WAITS))
        self.assertEqual((second['position'], second['queue'], second['position_label']), (2, 2, 'שניים בתור · 2 ממתינים'))
        self.assertTrue(second['sentence'].endswith('שניים בתור'))
        self.assertEqual(self.summary()['position_label'], 'ראשונים בתור · 2 ממתינים')

    def test_a_lesson_that_is_gone_is_said_from_the_copy(self):
        self.stand_ins()
        self.young_lesson.delete()
        summary = self.summary()
        self.assertEqual((summary['wanted']['exists'], summary['wanted']['label'], summary['wanted']['instructor']), (False, LABEL, ''))
        self.assertEqual(summary['seats'], {'enrolled': None, 'capacity': None, 'seats_left': None, 'is_full': False, 'seats_label': ''})
        self.assertEqual((summary['alternatives'], summary['alternatives_note']), ([], ''))
        self.assertTrue(summary['sentence'].startswith('רצו *קפוארה 4.5-6 יום שלישי* ביום שלישי 17:00'))

    def test_the_conversation_carries_the_same_summary(self):
        self.stand_ins()
        self.assertEqual(self.get(f'contacts/{self.lead.id}/').data['waiting_summary'], self.summary())

    def test_nobody_who_waits_for_nothing_has_one(self):
        self.incoming('שלום', phone=WAITS)
        other = self.contact(WAITS)
        self.assertIsNone(self.get(f'contacts/{other.id}/').data['waiting_summary'])
        self.assertIsNone(self.get(f'leads/{other.id}/').data['details']['waiting_summary'])
        # ... and nothing is read for it.
        with CaptureQueriesContext(connection) as seen:
            self.assertIsNone(waiting_summary.of(other))
        self.assertEqual(len(seen), 0)

    def test_the_summary_asks_the_same_number_of_questions_whatever_the_branch_holds(self):
        def questions():
            with CaptureQueriesContext(connection) as seen:
                waiting_summary.of(self.fresh())
            return len(seen)

        self.stand_ins()
        before = questions()
        for index in range(8):
            group = self.course(f'קפוארה 4.5-6 נוספת {index}')
            self.lesson(group, index % 5, 15)
            self.lesson(group, (index + 1) % 5, 16)
        self.assertEqual(questions(), before)
        self.assertLessEqual(before, 20)


class TheChildOfTheRowTests(SummaryCase):
    def test_the_card_that_opens_is_of_the_child_the_row_names(self):
        family = TestDataFactory.create_family(name='אלון', branch=self.branch, phone='050-5550101')
        older = TestDataFactory.create_child(family=family, first_name='יובל', last_name='אלון', status='active')
        younger = TestDataFactory.create_child(family=family, first_name='נועם', last_name='אלון', status='pending')
        Contact.objects.filter(pk=self.lead.pk).update(kogo_family_id=family.id, kogo_child_ids=[str(older.id), str(younger.id)])
        WaitingListEntry.objects.filter(pk=self.entry.pk).update(child='נועם, בן 5')
        row = self.get('waiting-list/').data['results'][0]
        self.assertEqual(row['customer'], {
            'family_id': str(family.id), 'child_id': str(younger.id), 'link': f'/customers?child={younger.id}',
        })
        self.assertEqual(self.summary()['customer'], row['customer'])
        self.assertEqual(self.get(f'leads/{self.lead.id}/').data['customer'], row['customer'])
        # No name in what was written: the first child the matching gave.
        WaitingListEntry.objects.filter(pk=self.entry.pk).update(child='בן 5')
        self.assertEqual(self.get('waiting-list/').data['results'][0]['customer']['child_id'], str(older.id))
        # On the list of conversations the contact has no row to speak of: the first child.
        self.assertEqual(self.get(f'contacts/{self.lead.id}/').data['customer']['child_id'], str(older.id))

    def test_somebody_the_system_does_not_know_has_no_card(self):
        self.assertIsNone(self.get('waiting-list/').data['results'][0]['customer'])
        self.assertEqual(state.now_israel_date(), timezone.localtime().date())
