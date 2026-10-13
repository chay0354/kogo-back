"""
The waiting list of a lesson that is full (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ד):
the bot's tool that only marks, the row that is written when an answer really
leaves, the office's own door, the lead stage, and the cron's look that tells
the office — and nobody else — when a seat came free.

"Full" is the registration form's own count throughout: the lesson of the
fixture gets a limit of one seat and one paying child, the way test_course_tools
fills a lesson.
"""
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from apps.core.tests.test_fixtures import TestDataFactory
from apps.enrollments.models import LessonEnrollment
from apps.wahub import cron, lead_stage, live_test, shadow, waiting_list
from apps.wahub.models import Contact, ContactEvent, Message, ShadowReply, WaitingListEntry
from apps.wahub.shadow_tools import ToolContext, summarize_result
from apps.wahub.tests.base import PHONE
from apps.wahub.tests.test_course_tools import CourseToolsTestCase

LABEL = 'קפוארה 4.5-6 יום שלישי · יום שלישי · 17:00 · קרל וגרטי קורי 8'
FREED = 'התפנה מקום בקפוארה 4.5-6 יום שלישי יום שלישי 17:00'


class WaitingListCase(CourseToolsTestCase):
    def setUp(self):
        super().setUp()
        # ManyChat has been copying the old bot's replies: an unanswered message means what it says.
        seen = patch.object(lead_stage, 'replies_copied_since', return_value=timezone.now() - timedelta(days=365))
        seen.start()
        self.addCleanup(seen.stop)
        self.incoming('היי, יש מקום בקפוארה ביום שלישי? הילד בן 5')
        self.bot_reply('בודקת')
        self.lead = self.contact()
        self.sitting = []

    def fill(self, lesson=None, seats=1):
        """The lesson holds `seats` children and every seat is taken."""
        lesson = lesson or self.young_lesson
        lesson.capacity = seats
        lesson.save(update_fields=['capacity'])
        for index in range(seats):
            _family, child = self.family_with_child(phone=f'05077700{len(self.sitting) + index:02d}')
            self.sitting.append(LessonEnrollment.objects.create(child=child, lesson=lesson, status='active'))
        return lesson

    def free_a_seat(self):
        row = self.sitting.pop()
        row.status = 'cancelled'
        row.save(update_fields=['status'])

    def fresh(self, contact=None) -> Contact:
        return Contact.objects.get(pk=(contact or self.lead).pk)

    def join(self, contact=None, lesson=None, **body):
        return self.post(f'contacts/{(contact or self.lead).id}/waiting-list/', {'lesson_id': str((lesson or self.young_lesson).id), **body})

    def journal(self, contact=None) -> list:
        return list(
            ContactEvent.objects.filter(contact=contact or self.lead, kind='waiting_list').order_by('id').values_list('text', flat=True)
        )


# --- the bot's tool ---------------------------------------------------------------------------------------

class ToolTests(WaitingListCase):
    def test_a_lesson_with_room_is_not_a_waiting_list(self):
        ctx = ToolContext(self.lead)
        result = self.tool('join_waiting_list', ctx, lesson_id=str(self.young_lesson.id), child='בן 5')
        self.assertEqual((result['ok'], result['has_room']), (False, True))
        self.assertNotIn('would_add', result)
        self.assertIn('עדיין אין רשומים', result['lesson']['seats_text'])
        self.assertEqual(ctx.waiting_list, [])
        self.assertEqual(summarize_result('join_waiting_list', result), f'יש מקום בשיעור — לא נוסף לרשימת המתנה ({LABEL})')

    def test_a_full_lesson_is_marked_and_nothing_is_written(self):
        self.fill()
        ctx = ToolContext(self.lead)
        result = self.tool('join_waiting_list', ctx, lesson_id=str(self.young_lesson.id), child='  בן 5,   גן חובה ')
        self.assertEqual((result['ok'], result['would_add']), (True, True))
        self.assertEqual(result['lesson'], {
            'id': str(self.young_lesson.id), 'course_id': str(self.young.id), 'course': 'קפוארה 4.5-6 יום שלישי',
            'day': 'שלישי', 'start': '17:00', 'end': '17:45', 'branch': 'קרל וגרטי קורי 8', 'label': LABEL,
            'seats_text': 'ביום שלישי ב-17:00 רשום 1 מתוך 1 מקומות, השיעור מלא',
        })
        self.assertEqual(ctx.waiting_list, [{'lesson_id': str(self.young_lesson.id), 'child': 'בן 5, גן חובה', 'label': LABEL}])
        # The tool only marks, as request_human does: no row, no journal, nothing on the contact.
        self.assertEqual(WaitingListEntry.objects.count(), 0)
        self.assertEqual((self.fresh().lead_waiting, self.journal()), ({}, []))
        self.assertEqual(summarize_result('join_waiting_list', result), f'סומן לרשימת ההמתנה: {LABEL}')

    def test_with_no_contact_at_all_it_answers_the_same(self):
        """ "נסה שאלה" and the bot's self-checks: no customer behind the question."""
        self.fill()
        result = self.tool('join_waiting_list', lesson_id=str(self.young_lesson.id))
        self.assertEqual((result['ok'], result['would_add']), (True, True))
        self.assertEqual(WaitingListEntry.objects.count(), 0)

    def test_somebody_already_on_the_list_is_told_so(self):
        self.fill()
        self.assertEqual(self.join().status_code, 201)
        ctx = ToolContext(self.lead)
        result = self.tool('join_waiting_list', ctx, lesson_id=str(self.young_lesson.id))
        self.assertEqual((result['ok'], result['already']), (True, True))
        self.assertEqual(ctx.waiting_list, [])
        self.assertEqual(WaitingListEntry.objects.count(), 1)

    def test_what_is_not_a_lesson_of_ours_is_refused_in_words(self):
        external = self.external_course.lessons.first()
        for lesson_id in ('', 'שלישי', '00000000-0000-0000-0000-000000000000', str(external.id), str(self.young.id)):
            with self.subTest(lesson_id=lesson_id):
                result = self.tool('join_waiting_list', lesson_id=lesson_id)
                self.assertFalse(result['ok'])
                self.assertTrue(result['error'])

    def test_a_shadow_reply_and_a_try_question_write_no_row(self):
        """The stub answers here (no Claude key); what matters is that drafting an answer never reaches the list."""
        self.fill()
        self.incoming('רק ביום שלישי אנחנו יכולים, אפשר רשימת המתנה?')
        shadow.propose(self.fresh(), now=timezone.now() + timedelta(minutes=1))
        shadow.answer('אפשר להיכנס לרשימת המתנה לקפוארה ביום שלישי בראש העין?')
        self.assertEqual(WaitingListEntry.objects.count(), 0)


class LiveAnswerTests(WaitingListCase):
    """The row is written where the answer really leaves: the live answer, once it was delivered."""

    def marks(self, lesson=None, child='בן 5'):
        return [
            {'name': 'find_courses', 'input': {'city': 'ראש העין'}, 'summary': '…'},
            {'name': 'join_waiting_list', 'input': {'lesson_id': str((lesson or self.young_lesson).id), 'child': child}, 'summary': '…'},
        ]

    def test_the_marks_of_an_answer_that_left_become_rows(self):
        self.fill()
        self.assertEqual(waiting_list.apply_bot_marks(self.lead, self.marks()), 1)
        entry = WaitingListEntry.objects.get()
        self.assertEqual((entry.contact_id, entry.lesson_id, entry.status, entry.added_by_kind, entry.added_by), (
            self.lead.id, self.young_lesson.id, 'waiting', 'bot', None,
        ))
        self.assertEqual((entry.course_name, entry.branch_name, entry.day_of_week, str(entry.start_time)[:5], entry.child), (
            'קפוארה 4.5-6 יום שלישי', 'קרל וגרטי קורי 8', 2, '17:00', 'בן 5',
        ))
        self.assertEqual(self.journal(), [f'הבוט הוסיף לרשימת ההמתנה: {LABEL} (בן 5)'])
        # Twice the same answer makes one row.
        self.assertEqual(waiting_list.apply_bot_marks(self.lead, self.marks()), 0)
        self.assertEqual(WaitingListEntry.objects.count(), 1)

    def test_a_seat_that_came_free_while_the_answer_was_written_makes_no_row(self):
        self.assertEqual(waiting_list.apply_bot_marks(self.lead, self.marks()), 0)
        self.assertEqual(WaitingListEntry.objects.count(), 0)

    def test_marks_that_are_not_marks_are_passed_over(self):
        self.fill()
        for tools in (None, [], 'x', [None, {'name': 'join_waiting_list'}, {'name': 'join_waiting_list', 'input': {'lesson_id': 'x'}}]):
            with self.subTest(tools=tools):
                self.assertEqual(waiting_list.apply_bot_marks(self.lead, tools), 0)
        self.assertEqual(WaitingListEntry.objects.count(), 0)

    def _live_answer(self, *, delivered: bool):
        Contact.objects.filter(pk=self.lead.pk).update(live_test=True)
        self.incoming('רק ביום שלישי אנחנו יכולים')

        def answer(contact, **_how):
            # The draft as shadow.answer_now keeps it: the tool call is on it, by name and input.
            return ShadowReply.objects.create(
                contact=contact, text='אתם ברשימת ההמתנה לשיעור הזה, ונעדכן כשיתפנה מקום', tools_used=self.marks(),
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

    def test_a_live_answer_that_was_delivered_writes_the_row(self):
        self.fill()
        self.assertEqual(self._live_answer(delivered=True)['outcome'], live_test.OUTCOME_SENT)
        self.assertEqual(WaitingListEntry.objects.get().added_by_kind, 'bot')
        self.assertEqual(self.fresh().lead_stage, 'waiting_list')

    def test_a_live_answer_that_did_not_reach_the_customer_writes_none(self):
        self.fill()
        self.assertEqual(self._live_answer(delivered=False)['outcome'], live_test.OUTCOME_FAILED)
        self.assertEqual(WaitingListEntry.objects.count(), 0)


# --- the office's own door, and the lists ------------------------------------------------------------------

class OfficeTests(WaitingListCase):
    def test_the_office_adds_somebody_and_it_is_one_row(self):
        self.fill()
        response = self.join(child='בן 5')
        self.assertEqual(response.status_code, 201)
        entry = response.data['entry']
        self.assertEqual(set(entry), {
            'id', 'contact_id', 'name', 'phone_display', 'is_demo', 'status', 'status_label', 'lesson', 'child', 'added_by',
            'added_by_name', 'created_at', 'freed_at', 'closed_at', 'closed_by_name', 'closed_reason', 'position', 'title',
        })
        self.assertEqual((entry['status'], entry['status_label'], entry['added_by'], entry['added_by_name']), ('waiting', 'ממתין', 'person', 'דנה מנהלת'))
        self.assertEqual((entry['position'], entry['title'], entry['child']), (1, f'ברשימת המתנה: {LABEL}', 'בן 5'))
        self.assertEqual(entry['lesson'], {
            'id': str(self.young_lesson.id), 'course_id': str(self.young.id), 'course': 'קפוארה 4.5-6 יום שלישי',
            'day': 'שלישי', 'start': '17:00', 'end': '17:45', 'branch': 'קרל וגרטי קורי 8', 'label': LABEL,
        })
        # The contact carries its place, in the conversations and in the leads alike.
        block = response.data['contact']['waiting_list']
        self.assertEqual(set(block), {
            'status', 'status_label', 'entry_id', 'lesson_id', 'label', 'course', 'day', 'start', 'branch', 'child', 'since',
            'freed_at', 'title', 'more',
        })
        self.assertEqual((block['status'], block['status_label'], block['label'], block['more']), ('waiting', 'ברשימת המתנה', LABEL, 0))
        self.assertEqual(self.journal(), [f'נוסף לרשימת ההמתנה: {LABEL} (בן 5)'])

        again = self.join()
        self.assertEqual((again.status_code, again.data['code'], again.data['entry']['id']), (409, 'exists', entry['id']))
        self.assertEqual(WaitingListEntry.objects.count(), 1)

    def test_a_lesson_with_room_or_no_lesson_is_refused_with_a_code(self):
        response = self.join()
        self.assertEqual((response.status_code, response.data['code']), (400, 'has_room'))
        self.assertIn('יש מקום', response.data['detail'])
        external = self.external_course.lessons.first()
        for lesson_id, code in (('x', 'bad_lesson'), ('', 'bad_lesson'), ('00000000-0000-0000-0000-000000000000', 'no_lesson'), (str(external.id), 'no_lesson')):
            with self.subTest(lesson_id=lesson_id):
                response = self.post(f'contacts/{self.lead.id}/waiting-list/', {'lesson_id': lesson_id})
                self.assertEqual((response.status_code, response.data['code']), (400, code))
        self.assertEqual(WaitingListEntry.objects.count(), 0)

    def test_the_whole_list_whose_seat_came_free_first(self):
        self.fill()
        self.fill(self.dance_lesson)
        other = Contact.objects.create(phone='972505550202', name='שני')
        first = self.join().data['entry']['id']
        second = self.join(other).data['entry']['id']
        third = self.join(other, self.dance_lesson).data['entry']['id']
        WaitingListEntry.objects.filter(pk=third).update(status='freed', freed_at=timezone.now())
        waiting_list.sync_contact(other.pk)

        data = self.get('waiting-list/').data
        self.assertEqual(data['counts'], {'waiting': 2, 'freed': 1})
        self.assertEqual([(row['id'], row['position']) for row in data['results']], [(third, 1), (first, 1), (second, 2)])
        self.assertEqual([row['id'] for row in self.get('waiting-list/', status='freed').data['results']], [third])
        self.assertEqual([row['id'] for row in self.get('waiting-list/', lesson=str(self.young_lesson.id)).data['results']], [first, second])
        self.assertEqual(self.get('waiting-list/', lesson='x').data['results'], [])
        self.assertEqual(self.get('waiting-list/', status='closed').data['results'], [])
        self.assertEqual(self.get('waiting-list/', status='nonsense').status_code, 400)
        # The contact who waits for two lessons carries the one whose seat came free, and says there is another.
        block = self.get(f'contacts/{other.id}/').data['waiting_list']
        self.assertEqual((block['status'], block['entry_id'], block['more']), ('freed', third, 1))

    def test_handled_and_removed_take_a_row_off_the_list(self):
        self.fill()
        entry_id = self.join().data['entry']['id']
        response = self.post(f'waiting-list/{entry_id}/handled/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.data['entry']['status'], response.data['entry']['closed_by_name']), ('handled', 'דנה מנהלת'))
        self.assertIsNone(response.data['contact']['waiting_list'])
        self.assertEqual(self.fresh().lead_waiting, {})
        # A row that is already off the list comes back as it is.
        again = self.post(f'waiting-list/{entry_id}/remove/')
        self.assertEqual((again.status_code, again.data['entry']['status']), (200, 'handled'))

        second = self.join().data['entry']['id']
        self.assertNotEqual(second, entry_id)                # off the list, the contact may wait for the lesson again
        removed = self.post(f'waiting-list/{second}/remove/')
        self.assertEqual(removed.data['entry']['status'], 'removed')
        self.assertEqual(self.journal()[-1], f'הוסר מרשימת ההמתנה: {LABEL}')
        self.assertEqual(self.post('waiting-list/999999/handled/').status_code, 404)

    def test_the_lead_shows_the_lesson_and_every_row(self):
        self.fill()
        self.join(child='בן 5')
        row = self.get(f'leads/{self.lead.id}/').data
        self.assertEqual((row['stage'], row['stage_label'], row['owner']), ('waiting_list', 'ברשימת המתנה', ''))
        self.assertEqual((row['next']['who'], row['next']['what']), ('none', 'ברשימת המתנה — נתריע כשיתפנה מקום'))
        self.assertEqual((row['waiting_list']['label'], row['waiting_list']['status']), (LABEL, 'waiting'))
        self.assertEqual([entry['status'] for entry in row['details']['waiting_list_entries']], ['waiting'])
        self.assertEqual(row['details']['reason'], f'ברשימת המתנה: {LABEL}.')
        listed = self.get('leads/', owner='bot', stage='waiting_list').data
        self.assertEqual([lead['id'] for lead in listed['results']], [self.lead.id])
        self.assertIn({'key': 'waiting_list', 'label': 'ברשימת המתנה', 'count': 1}, listed['stages'])

    def test_a_lead_a_person_closed_waits_for_nothing(self):
        self.fill()
        self.join()
        self.post(f'leads/{self.lead.id}/close/', {'reason': 'not_interested'})
        entry = WaitingListEntry.objects.get()
        self.assertEqual((entry.status, entry.closed_reason, entry.closed_by), ('removed', 'הליד נסגר', self.manager))
        self.assertEqual((self.fresh().lead_waiting, self.fresh().lead_stage), ({}, 'not_interested'))


# --- the lead stage ---------------------------------------------------------------------------------------

class StageTests(WaitingListCase):
    def waiting(self, **fields) -> Contact:
        self.fill()
        self.join()
        if fields:
            Contact.objects.filter(pk=self.lead.pk).update(**fields)
            lead_stage.refresh(self.lead.pk)
        return self.fresh()

    def test_it_stands_above_nothing_to_offer_and_everything_after_it(self):
        full = {'blocker': 'full', 'blocker_text': 'אין מקום בחוג', 'intent': 'class'}
        contact = self.waiting(lead_ai=full, analyzed_at=timezone.now() + timedelta(minutes=1))
        self.assertEqual(contact.lead_stage, 'waiting_list')
        WaitingListEntry.objects.update(status='removed')
        waiting_list.sync_contact(contact.pk)
        self.assertEqual(self.fresh().lead_stage, 'nothing_to_offer')

    def test_a_customer_or_somebody_registered_elsewhere_still_waits(self):
        for outcome in ('customer_before', 'registered_after'):
            with self.subTest(outcome=outcome):
                Contact.objects.filter(pk=self.lead.pk).update(kogo_outcome=outcome)
                contact = self.waiting() if not WaitingListEntry.objects.exists() else self.fresh()
                lead_stage.refresh(contact.pk)
                self.assertEqual(self.fresh().lead_stage, 'waiting_list')

    def test_what_asks_for_a_person_comes_first(self):
        contact = self.waiting(needs_human=True, needs_human_reason='ביקש נציג')
        self.assertEqual((contact.lead_stage, contact.lead_owner), ('needs_reply', 'office'))
        # …and it is still on the list, for whoever opens the conversation.
        self.assertEqual(self.get(f'contacts/{contact.id}/').data['waiting_list']['status'], 'waiting')

    def test_a_person_who_holds_the_conversation_has_nothing_to_do_while_it_waits(self):
        contact = self.waiting(handled_by='human')
        self.assertEqual((contact.lead_stage, contact.lead_owner), ('waiting_list', ''))


# --- the cron's look ----------------------------------------------------------------------------------------

class CheckTests(WaitingListCase):
    def setUp(self):
        super().setUp()
        self.fill()
        self.entry_id = self.join(child='בן 5').data['entry']['id']

    def entry(self) -> WaitingListEntry:
        return WaitingListEntry.objects.get(pk=self.entry_id)

    def test_while_the_lesson_is_full_nothing_happens(self):
        counts = waiting_list.check()
        self.assertEqual(counts, {'waiting_freed': 0, 'waiting_closed': 0, 'waiting_checked': 1})
        self.assertEqual(self.entry().status, 'waiting')
        self.assertIsNotNone(self.entry().checked_at)
        self.assertEqual(self.fresh().lead_owner, '')

    def test_full_then_a_seat_comes_free_the_office_is_told_and_marks_it_handled(self):
        """The whole road: full → a seat free → the lead waits for the office → handled. Nothing is sent to anybody."""
        messages = Message.objects.count()
        self.free_a_seat()
        with patch('apps.core.office_alerts.raise_office_alert', side_effect=AssertionError('the office was alerted on WhatsApp')):
            counts = waiting_list.check()
        self.assertEqual((counts['waiting_freed'], counts['waiting_closed']), (1, 0))
        entry = self.entry()
        self.assertEqual(entry.status, 'freed')
        self.assertIsNotNone(entry.freed_at)
        self.assertEqual(self.journal()[-1], f'{FREED} (קרל וגרטי קורי 8)')

        row = self.get(f'leads/{self.lead.id}/').data
        self.assertEqual((row['stage'], row['owner'], row['next']['who']), ('waiting_list', 'office', 'office'))
        self.assertEqual(row['next']['what'], 'להציע ללקוח את המקום שהתפנה')
        self.assertEqual(row['summary'], f'{FREED} (קרל וגרטי קורי 8).')
        self.assertEqual((row['waiting_list']['status'], row['waiting_list']['status_label'], row['waiting_list']['title']), ('freed', 'התפנה מקום', FREED))
        self.assertIn(self.lead.id, [lead['id'] for lead in self.get('leads/', owner='office').data['results']])
        self.assertEqual(self.get('waiting-list/').data['counts'], {'waiting': 0, 'freed': 1})
        # Only inside the system: the red badge of the menu is "asks for a person" alone, and no message was written.
        self.assertEqual(self.get('contacts/counts/').data['boxes']['needs_human'], 0)
        self.assertFalse(self.get(f'contacts/{self.lead.id}/').data['chat']['open'])
        self.assertEqual(Message.objects.count(), messages)
        self.deliver.assert_not_called()

        handled = self.post(f'waiting-list/{self.entry_id}/handled/')
        self.assertEqual(handled.data['entry']['status'], 'handled')
        after = self.get(f'leads/{self.lead.id}/').data
        self.assertNotEqual(after['stage'], 'waiting_list')
        self.assertNotEqual(after['owner'], 'office')
        self.assertIsNone(after['waiting_list'])

    def test_a_seat_that_was_taken_again_goes_back_to_waiting(self):
        self.free_a_seat()
        waiting_list.check()
        self.assertEqual(self.fresh().lead_owner, 'office')
        self.fill(seats=1)
        waiting_list.check()
        self.assertEqual((self.entry().status, self.entry().freed_at), ('waiting', None))
        self.assertEqual((self.fresh().lead_stage, self.fresh().lead_owner), ('waiting_list', ''))
        self.assertEqual(self.journal()[-1], f'המקום נתפס שוב — חזר לרשימת ההמתנה: {LABEL}')

    def test_everybody_who_waits_for_the_lesson_is_told_in_the_order_they_came(self):
        other = Contact.objects.create(phone='972505550202', name='שני')
        self.join(other)
        self.free_a_seat()
        self.assertEqual(waiting_list.check()['waiting_freed'], 2)
        rows = self.get('waiting-list/', status='freed').data['results']
        self.assertEqual([(row['contact_id'], row['position']) for row in rows], [(self.lead.id, 1), (other.id, 2)])

    def test_a_child_who_registered_to_the_lesson_leaves_the_list(self):
        _family, child = self.family_with_child(phone='0507778899')
        Contact.objects.filter(pk=self.lead.pk).update(kogo_child_ids=[str(child.id)])
        self.young_lesson.capacity = 2
        self.young_lesson.save(update_fields=['capacity'])
        LessonEnrollment.objects.create(child=child, lesson=self.young_lesson, status='active')
        self.assertEqual(waiting_list.check()['waiting_closed'], 1)
        self.assertEqual((self.entry().status, self.entry().closed_reason), ('handled', 'נרשם לשיעור'))
        self.assertEqual(self.fresh().lead_waiting, {})

    def test_a_lesson_that_is_gone_or_cancelled_closes_the_row_with_the_reason(self):
        self.young_lesson.status = 'cancelled'
        self.young_lesson.save(update_fields=['status'])
        waiting_list.check()
        self.assertEqual((self.entry().status, self.entry().closed_reason), ('removed', 'השיעור כבר לא מתקיים'))

        self.fill(self.dance_lesson)
        second = self.join(lesson=self.dance_lesson).data['entry']['id']
        LessonEnrollment.objects.filter(lesson=self.dance_lesson).delete()
        self.dance_lesson.delete()
        waiting_list.check()
        gone = WaitingListEntry.objects.get(pk=second)
        self.assertEqual((gone.status, gone.closed_reason), ('removed', 'השיעור נמחק'))
        # The row still says what was waited for.
        self.assertEqual(waiting_list.lesson_block(gone)['label'], 'א-ב ריקוד · יום שני · 17:00 · קרל וגרטי קורי 8')
        self.assertEqual(self.fresh().lead_waiting, {})

    def test_the_cron_runs_the_look_and_sends_nothing(self):
        self.free_a_seat()
        counts = cron.tick(budget_seconds=20)
        self.assertEqual((counts['waiting_freed'], counts['waiting_closed']), (1, 0))
        self.assertEqual(self.fresh().lead_owner, 'office')
        self.deliver.assert_not_called()

    def test_a_look_that_runs_out_of_time_loses_nothing(self):
        self.free_a_seat()
        self.assertEqual(waiting_list.check(budget_seconds=-1), {'waiting_freed': 0, 'waiting_closed': 0, 'waiting_checked': 0})
        self.assertEqual(self.entry().status, 'waiting')
        self.assertEqual(waiting_list.check()['waiting_freed'], 1)

    def test_a_demo_contacts_row_says_it_is_a_demo(self):
        demo = Contact.objects.create(phone='972505550303', name='דמו', is_demo=True, source='demo')
        entry = self.join(demo).data['entry']
        self.assertTrue(entry['is_demo'])


class TheBotsOwnWordsTests(WaitingListCase):
    def test_the_tool_and_the_rule_are_in_what_the_bot_is_given(self):
        from apps.wahub import knowledge_seed, shadow_tools
        from apps.wahub.evals import rules, scenarios

        tool = next(tool for tool in shadow_tools.TOOLS if tool['name'] == 'join_waiting_list')
        self.assertEqual(tool['input_schema']['required'], ['lesson_id'])
        self.assertIn('לא מבטיחים מקום', tool['description'])
        rule = next(record for record in knowledge_seed.BEHAVIOR_RULES if record['title'] == 'שיעור מלא — מועד אחר, ואז רשימת המתנה')
        self.assertIn('בעל המערכת 13.10.2026', rule['source_note'])
        self.assertIn('join_waiting_list', rule['body'])
        self.assertIn('WL1', rules.RULES)
        self.assertIn('WL1', next(scenario.rules for scenario in scenarios.SCENARIOS if scenario.id == 'waiting_list_full_lesson'))
        self.assertEqual(PHONE, self.lead.phone)
