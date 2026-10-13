"""
A conversation that waits for a person more than three hours of the office's own time
(docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ט.2): how the hours are counted — the night, the weekend and a closed
day are not — who counts as waiting for a person, the flag and the mark on every row, the order of the list,
the count, and what the live update says by the clock alone.

The clock is the test's (tests/test_leads_v2.py). 12.10.2026 is a Monday.
"""
from datetime import date, datetime, timedelta

from django.utils import timezone

from apps.wahub import knowledge, office_clock, queries
from apps.wahub.models import Contact
from apps.wahub.tests.test_leads_v2 import ZONE, LeadsCase, special_day

MINUTE, HOUR = timedelta(minutes=1), timedelta(hours=1)


def at(day, hour, minute=0):
    return timezone.make_aware(datetime(2026, 10, day, hour, minute), ZONE)


def moment(value):
    """A moment as an answer writes it, to hold against one of the test's own."""
    return datetime.fromisoformat(value) if value else None


class ClockTests(LeadsCase):
    """10:30–18:00, Sunday to Thursday, until somebody files other hours."""

    def waited(self, since, now) -> int:
        return office_clock.Clock(now).waited(since)

    def test_the_night_is_not_counted(self):
        # Wrote on Tuesday at 20:00, read on Wednesday at 11:00: half an hour.
        self.assertEqual(self.waited(at(13, 20), at(14, 11)), 30)
        # Wrote at 16:00, read the next morning at 11:30: two hours and one — exactly at the line.
        self.assertEqual(self.waited(at(13, 16), at(14, 11, 30)), 180)
        self.assertEqual(self.waited(at(13, 16), at(14, 11, 29)), 179)

    def test_the_weekend_is_not_counted(self):
        # Thursday 17:00 to Sunday 12:30: one hour and two.
        self.assertEqual(self.waited(at(15, 17), at(18, 12, 30)), 180)
        self.assertEqual(self.waited(at(16, 11), at(17, 23)), 0)               # all of a Friday and a Saturday

    def test_inside_one_day(self):
        self.assertEqual(self.waited(at(18, 11), at(18, 14, 1)), 181)
        self.assertEqual(self.waited(at(18, 9), at(18, 10, 45)), 15)            # before the office opened
        self.assertEqual(self.waited(at(18, 17, 30), at(18, 23)), 30)           # after it closed
        self.assertIsNone(self.waited(None, at(18, 12)))

    def test_a_closed_day_counts_for_nothing_and_other_hours_count_as_they_are(self):
        special_day('חג', date(2026, 10, 13), date(2026, 10, 14))               # Tuesday and Wednesday closed
        self.assertEqual(self.waited(at(12, 17, 30), at(15, 12, 30)), 150)      # Monday's last half hour, Thursday's first two
        knowledge.create_item({
            'kind': 'special_day', 'title': 'ערב חג', 'date_from': '2026-10-15', 'date_to': '2026-10-15',
            'state': 'hours', 'hours_from': '09:00', 'hours_to': '13:00',
        })
        office_clock.cache.clear()
        self.assertEqual(self.waited(at(12, 17, 30), at(15, 12, 30)), 30 + 210)  # Thursday opens at 9:00 now
        self.assertEqual(self.waited(at(15, 12), at(15, 16)), 60)               # ... and closes at 13:00

    def test_the_hours_filed_in_the_knowledge_are_the_ones_counted(self):
        week = {day: {'open': True, 'from': '08:00', 'to': '12:00'} for day in ('sun', 'mon', 'tue', 'wed', 'thu', 'fri')}
        week['sat'] = {'open': False}
        knowledge.create_item({'kind': 'office_hours', 'title': 'שעות', 'weekly': week})
        self.assertEqual(self.waited(at(15, 11), at(16, 9)), 60 + 60)           # a Friday the office itself says it works

    def test_the_moment_from_which_three_hours_have_passed(self):
        self.assertEqual(office_clock.Clock(at(14, 15)).cutoff(), at(14, 12))
        self.assertEqual(office_clock.Clock(at(14, 11, 30)).cutoff(), at(13, 16))           # across the night
        self.assertEqual(office_clock.Clock(at(18, 12, 30)).cutoff(), at(15, 17))           # across the weekend
        # Whoever has waited from that moment has waited exactly three hours; a minute later — a minute less.
        clock = office_clock.Clock(at(18, 12, 30))
        self.assertEqual((clock.waited(clock.cutoff()), clock.waited(clock.cutoff() + MINUTE)), (180, 179))
        # A month of closed days: nobody can have waited three office hours.
        special_day('סגור', date(2026, 9, 1), date(2026, 10, 31))
        office_clock.cache.clear()
        self.assertIsNone(office_clock.Clock(at(18, 12, 30)).cutoff())

    def test_what_the_mark_says(self):
        open_now, closed_now = office_clock.Clock(at(14, 15)), office_clock.Clock(at(14, 20))
        self.assertEqual((open_now.open_now, closed_now.open_now), (True, False))
        self.assertEqual(open_now.label(200), 'מחכה 3:20 שע׳')
        self.assertEqual(open_now.label(60), 'מחכה 1:00 שע׳')
        self.assertEqual(open_now.label(40), 'מחכה 40 דק׳')
        self.assertEqual((open_now.label(0), closed_now.label(0)), ('כתב עכשיו', 'כתב מחוץ לשעות המשרד'))

    def test_the_hours_are_read_once_a_minute_not_on_every_call(self):
        with self.assertNumQueries(2):          # the office hours, the special days
            office_clock.Clock(at(14, 15))
        with self.assertNumQueries(0):
            office_clock.Clock(at(14, 15)).cutoff()


class WaitingForAPersonCase(LeadsCase):
    """Wednesday 14.10.2026, 15:00 — the office has been open four and a half hours today."""

    def setUp(self):
        super().setUp()
        self.clock.to(2026, 10, 14, 15, 0)
        self.made_rows = 0

    def row(self, **fields) -> Contact:
        self.made_rows += 1
        base = dict(name=f'איש קשר {self.made_rows}', last_message_at=self.clock() - 10 * MINUTE * self.made_rows)
        base.update(fields)
        return Contact.objects.create(phone=f'9725055577{self.made_rows:02d}', **base)

    def asked_for_a_person(self, since, **fields) -> Contact:
        """Asked for a person at `since`; the old bot answered "a person will get back to you" a moment later."""
        return self.row(
            needs_human=True, needs_human_at=since, needs_human_reason='ביקש נציג', last_inbound_at=since,
            last_message_at=since + MINUTE, last_message_direction='out', last_message_sender='bot', **fields,
        )

    def held_and_waiting(self, since, **fields) -> Contact:
        """A person holds the conversation and the customer's message of `since` has no answer."""
        return self.row(
            handled_by='human', waiting_since=since, last_inbound_at=since, last_message_at=since,
            last_message_direction='in', last_message_sender='customer', **fields,
        )

    def chat(self, contact) -> dict:
        return self.get(f'contacts/{contact.id}/').data['chat']


class WhoWaitsTests(WaitingForAPersonCase):
    def test_asked_for_a_person_and_only_the_bot_answered(self):
        chat = self.chat(self.asked_for_a_person(at(14, 11)))
        self.assertEqual(
            (chat['overdue'], chat['waited_office_minutes'], chat['waiting_label'], moment(chat['waiting_from'])),
            (True, 240, 'מחכה 4:00 שע׳', at(14, 11)),
        )
        # An hour ago: waiting, and not yet past the line.
        chat = self.chat(self.asked_for_a_person(at(14, 14)))
        self.assertEqual((chat['overdue'], chat['waited_office_minutes'], chat['waiting_label']), (False, 60, 'מחכה 1:00 שע׳'))

    def test_the_request_made_last_night_has_waited_only_since_the_office_opened(self):
        chat = self.chat(self.asked_for_a_person(at(13, 20)))
        self.assertEqual((chat['overdue'], chat['waited_office_minutes']), (True, 270))
        self.clock.to(2026, 10, 14, 11, 0)
        chat = self.chat(Contact.objects.latest('id'))
        self.assertEqual((chat['overdue'], chat['waited_office_minutes'], chat['waiting_label']), (False, 30, 'מחכה 30 דק׳'))

    def test_an_answer_of_the_office_stops_the_clock_and_a_new_message_starts_it(self):
        contact = self.asked_for_a_person(at(14, 11))
        Contact.objects.filter(pk=contact.pk).update(
            last_message_at=at(14, 11, 30), last_message_direction='out', last_message_sender='office',
        )
        chat = self.chat(contact)
        self.assertEqual((chat['overdue'], chat['waited_office_minutes'], chat['waiting_label'], chat['waiting_from']), (False, None, '', None))
        # Still open — "מבקש נציג" stays until somebody closes it — only nobody is waiting.
        self.assertTrue(chat['open'])
        # The customer writes again and nobody answers: waiting again.
        Contact.objects.filter(pk=contact.pk).update(
            waiting_since=at(14, 14), last_inbound_at=at(14, 14), last_message_at=at(14, 14),
            last_message_direction='in', last_message_sender='customer',
        )
        self.assertTrue(self.chat(contact)['overdue'])

    def test_a_person_holds_it_and_the_customer_wrote(self):
        chat = self.chat(self.held_and_waiting(at(13, 17, 30)))
        self.assertEqual((chat['overdue'], chat['waited_office_minutes'], chat['open_reason']), (True, 300, 'human'))
        # Held, and the person's own message is the last word: nobody waits.
        answered = self.row(handled_by='human', last_message_direction='out', last_message_sender='office')
        self.assertEqual((self.chat(answered)['overdue'], self.chat(answered)['waiting_label']), (False, ''))

    def test_a_conversation_the_bot_handles_is_never_overdue(self):
        contact = self.row(waiting_since=at(13, 11), last_inbound_at=at(13, 11), last_message_direction='in', last_message_sender='customer')
        chat = self.chat(contact)
        self.assertEqual((chat['open'], chat['overdue'], chat['waited_office_minutes'], chat['waiting_label']), (False, False, None, ''))
        # ... the screen keeps its own mark for it, from the same field as before.
        self.assertEqual(moment(chat['waiting_since']), at(13, 11))

    def test_future_leads(self):
        today = self.clock().date()
        parked = self.row(
            handled_by='human', followup_status='later', followup_due=today + timedelta(days=30), followup_at=at(12, 12),
        )
        due_and_quiet = self.row(handled_by='human', followup_status='later', followup_due=today, followup_at=at(12, 12))
        wrote = self.held_and_waiting(
            at(14, 11), followup_status='later', followup_due=today + timedelta(days=30), followup_at=at(12, 12),
        )
        self.assertEqual([self.chat(contact)['overdue'] for contact in (parked, due_and_quiet, wrote)], [False, False, True])
        self.assertEqual(self.chat(wrote)['open_reason'], 'future_wrote')

    def test_what_stops_the_clock(self):
        contact = self.asked_for_a_person(at(14, 11))
        self.assertTrue(self.chat(contact)['overdue'])
        handled = self.post(f'contacts/{contact.id}/needs-human/', {'needs_human': False}).data['chat']
        self.assertEqual((handled['overdue'], handled['waiting_label']), (False, ''))
        other = self.asked_for_a_person(at(14, 11), is_demo=True, source='demo')
        closed = self.post(f'contacts/{other.id}/close/').data['chat']
        self.assertEqual((closed['open'], closed['overdue']), (False, False))


class ListTests(WaitingForAPersonCase):
    def setUp(self):
        super().setUp()
        self.newest = self.row(last_message_at=self.clock() - MINUTE)                                   # the bot's, talking now
        self.asked = self.asked_for_a_person(at(14, 11))                                                # four hours
        self.held = self.held_and_waiting(at(13, 17, 30))                                               # five hours
        self.fresh_request = self.asked_for_a_person(at(14, 14))                                        # one hour: not yet
        self.bots = self.row(waiting_since=at(13, 11), last_inbound_at=at(13, 11), last_message_at=at(13, 11),
                             last_message_direction='in', last_message_sender='customer')

    def ids(self, **params) -> list:
        return [row['id'] for row in self.get('contacts/', **params).data['results']]

    def test_whoever_is_overdue_is_pinned_to_the_head_longest_first_and_the_rest_keep_their_order(self):
        everybody = self.ids()
        self.assertEqual(everybody[:2], [self.held.id, self.asked.id])
        rest = everybody[2:]
        self.assertEqual(rest, [self.newest.id, self.fresh_request.id, self.bots.id])       # newest message first, as before
        # In every box they are in: "פתוחות", "מבקשים נציג".
        self.assertEqual(self.ids(box='open'), [self.held.id, self.asked.id, self.fresh_request.id])
        self.assertEqual(self.ids(box='needs_human'), [self.asked.id, self.fresh_request.id])
        self.assertEqual(self.ids(box='overdue'), [self.held.id, self.asked.id])
        # The leads view keeps its own fixed order.
        self.assertNotEqual(self.ids(view='leads', show_hidden=1)[:2], [self.held.id, self.asked.id])

    def test_a_search_keeps_its_own_order(self):
        Contact.objects.filter(pk=self.asked.pk).update(name='דנה לוי')
        Contact.objects.filter(pk=self.newest.pk).update(name='דנה כהן')
        self.assertEqual(set(self.ids(search='דנה', box='all')), {self.asked.id, self.newest.id})
        self.assertEqual(self.ids(search='דנה', box='all')[0], self.newest.id)

    def test_the_count_is_one_of_the_boxes(self):
        self.assertEqual(self.get('contacts/counts/').data['boxes']['overdue'], 2)
        self.assertEqual(queries.box_counts()['overdue'], 2)

    def test_the_live_update_says_who_crossed_the_line_though_nobody_touched_them(self):
        self.clock.forward(minutes=1)                                       # 15:01 — the rows were made a minute ago
        cursor = self.get('contacts/updates/').data['cursor']
        data = self.get('contacts/updates/', since=cursor).data
        self.assertEqual(data['contacts'], [])                              # nothing was touched
        self.assertEqual(data['boxes']['overdue'], 2)
        self.assertEqual([{**row, 'waiting_from': moment(row['waiting_from'])} for row in data['overdue']], [
            {'id': self.held.id, 'waited_office_minutes': 301, 'waiting_label': 'מחכה 5:01 שע׳', 'waiting_from': at(13, 17, 30)},
            {'id': self.asked.id, 'waited_office_minutes': 241, 'waiting_label': 'מחכה 4:01 שע׳', 'waiting_from': at(14, 11)},
        ])
        # Two hours later, with nothing touched, the one who asked at 14:00 is past the line too.
        self.clock.forward(hours=2)
        data = self.get('contacts/updates/', since=cursor).data
        self.assertEqual(data['contacts'], [])
        self.assertEqual([row['id'] for row in data['overdue']], [self.held.id, self.asked.id, self.fresh_request.id])
        self.assertEqual(data['boxes']['overdue'], 3)
        # After closing time nothing moves any more: at half past eleven at night the minutes are those of 18:00.
        self.clock.to(2026, 10, 14, 23, 30)
        at_night = {row['id']: row['waited_office_minutes'] for row in self.get('contacts/updates/', since=cursor).data['overdue']}
        self.assertEqual(at_night, {self.held.id: 480, self.asked.id: 420, self.fresh_request.id: 240})

    def test_nobody_overdue_is_an_empty_list(self):
        Contact.objects.all().update(needs_human=False, handled_by='bot')
        data = self.get('contacts/updates/').data
        self.assertEqual((data['boxes']['overdue'], data['overdue']), (0, []))
        self.assertEqual(self.ids(box='overdue'), [])

    def test_the_list_and_the_update_are_still_a_fixed_handful_of_queries(self):
        office_clock.calendar()                 # the office hours: read once a minute, whoever asks
        with self.assertNumQueries(5):          # the user, the profile, the count, the rows, their tags
            self.get('contacts/')
        for _ in range(6):
            self.asked_for_a_person(at(14, 11))
        with self.assertNumQueries(5):
            self.get('contacts/')
        self.clock.forward(minutes=1)
        cursor = self.get('contacts/updates/').data['cursor']
        with self.assertNumQueries(5):          # the user, the profile, what was touched, the counts, who is overdue
            self.get('contacts/updates/', since=cursor)

    def test_what_the_database_holds_agrees_with_what_each_row_says(self):
        """The order and the count compare columns to one moment; a row works its own minutes out. They must agree."""
        clock = office_clock.Clock(self.clock())
        today = self.clock().date()
        by_query = set(Contact.objects.filter(queries.overdue_q(today, clock.cutoff())).values_list('id', flat=True))
        by_row = {
            contact.id for contact in Contact.objects.all() if queries.waiting_block(contact, today, clock)['overdue']
        }
        self.assertEqual(by_query, by_row)
        self.assertEqual(by_query, {self.held.id, self.asked.id})


class LeadRowTests(WaitingForAPersonCase):
    def test_a_lead_that_waits_for_the_office_carries_the_same_flag(self):
        contact = self.lead(4 * HOUR, answer=None, needs_human=True, needs_human_at=at(14, 11), needs_human_reason='ביקש נציג')
        contact = self.staged(contact)
        self.assertEqual(contact.lead_owner, 'office')
        row = next(row for row in self.get('leads/', owner='office').data['results'] if row['contact_id'] == contact.id)
        self.assertEqual(
            (row['overdue'], row['waited_office_minutes'], row['waiting_label'], moment(row['waiting_from'])),
            (True, 240, 'מחכה 4:00 שע׳', at(14, 11)),
        )
        self.assertTrue(self.get(f'leads/{contact.id}/').data['overdue'])
        # A lead the bot handles says nothing of the kind.
        quiet = self.staged(self.lead(HOUR))
        row = next(row for row in self.get('leads/', owner='all').data['results'] if row['contact_id'] == quiet.id)
        self.assertEqual((row['overdue'], row['waited_office_minutes'], row['waiting_label']), (False, None, ''))
