"""
A child who came to a lesson without being registered, whose phone the
instructor wrote down on the attendance screen, is a lead (apps/wahub/walk_ins.py;
the owner, 14.10.2026): the phone becomes a contact, the registrations read it as
a lesson the child came to unregistered, the bot comes back with a message of its
own ("שמחנו לראות אתכם אצלנו בשיעור!") and then with the trial's round — and
through the very doors every follow-up goes through.

The walk-in is made the way the attendance screen makes it
(ghost_students.create_ghost_enrollment). ManyChat is the fake of
test_lead_automation.py; `self.network` fails any test that reaches for the real
one. The clock is the test's: Monday 12.10.2026, noon.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import override_settings

from apps.core.tests.test_fixtures import TestDataFactory
from apps.customers.models import Family, Payment
from apps.enrollments.ghost_students import create_ghost_enrollment, delete_ghost_enrollment, set_ghost_contact
from apps.enrollments.models import LessonAttendance, LessonEnrollment
from apps.wahub import (
    cron, inbound, lead_automation, lead_followup_seed, lead_stage, lead_templates, matching, settings_store, shadow_tools,
    walk_ins,
)
from apps.wahub.models import EVENT_CREATED, WALK_IN_LABEL, Contact, ContactEvent, KnowledgeItem, LeadFollowUp
from apps.wahub.shadow_tools import ToolContext
from apps.wahub.tests.test_lead_automation import AutomationCase, flow_of
from apps.wahub.tests.test_lead_followups import SENDING_ON, at
from apps.wahub.tests.test_leads_v2 import DAY, TODAY

PHONE = '972505551234'
LOCAL = '050-5551234'
# The owner's template, word for word (kogo_lead_walkin_1, 14.10.2026 — no variables).
WALK_IN_TEXT = 'היי, כאן קוגומלו 😊\nשמחנו לראות אתכם אצלנו בשיעור!\nרוצים לשריין מקום קבוע בקבוצה? אפשר לענות כאן ונעזור בהרשמה'
YESTERDAY = TODAY - timedelta(days=1)


class WalkInCase(AutomationCase):
    def setUp(self):
        super().setUp()
        self.recheck.side_effect = self.read_registrations          # the registrations are read for real here
        self.branch = TestDataFactory.create_branch(name='מינץ', city=TestDataFactory.create_city('פתח תקווה'))
        self.course = TestDataFactory.create_course(
            name='קפוארה גן חובה', branch=self.branch, course_type=TestDataFactory.create_course_type('קפוארה'),
        )
        self.instructor = TestDataFactory.create_instructor(first_name='יואב', last_name='כהן', branch=self.branch)
        self.lesson = TestDataFactory.create_lesson(course=self.course, instructor=self.instructor)

    def walk_in(self, day=TODAY, phone=LOCAL, first='דני', last='לוי'):
        """The instructor adds a child on the register of `day`, with the parent's phone: the screen's own call."""
        enrollment, _created = create_ghost_enrollment(
            lesson=self.lesson, first_name=first, last_name=last, phone=phone, occurrence_date=day,
        )
        return enrollment.child

    def found(self) -> Contact:
        return Contact.objects.get(phone=PHONE)

    def brought_in(self, day=TODAY) -> Contact:
        """A walk-in, brought in by the cron's look, with a ManyChat contact behind the phone (as a send would find)."""
        self.walk_in(day)
        walk_ins.find()
        Contact.objects.filter(phone=PHONE).update(manychat_subscriber_id='4242')
        return self.found()

    def family_pays(self, first='נועה'):
        family = TestDataFactory.create_family(name='לוי', branch=self.branch, phone=LOCAL)
        child = TestDataFactory.create_child(family=family, first_name=first, last_name='לוי', status='active')
        LessonEnrollment.objects.create(child=child, lesson=self.lesson, status='active')
        Payment.objects.create(
            child=child, family=family, lesson=self.lesson, payment_type='recurring_subscription', status='completed',
            base_amount=Decimal('260'), final_amount=Decimal('260'), description='מנוי חודשי - קפוארה',
            payment_date=self.clock(),
        )
        return child

    def real_child(self, status, first='נועה'):
        family = TestDataFactory.create_family(name='לוי', branch=self.branch, phone=LOCAL)
        return TestDataFactory.create_child(family=family, first_name=first, last_name='לוי', status=status)


# --- what the registrations say -----------------------------------------------------------------------------

class MatchingTests(WalkInCase):
    def test_a_walk_in_marked_present_came_to_a_lesson_without_registering(self):
        child = self.walk_in(YESTERDAY)
        found = matching.match_phone(PHONE)
        self.assertEqual(found.outcome, 'trial_only')
        self.assertEqual(found.detail, 'הגיע לשיעור קפוארה גן חובה ב-11.10 בלי להירשם · דני לוי · נוסף על ידי המדריך')
        self.assertEqual(found.trial, {
            'on': '2026-10-11', 'outcome': 'attended', 'course': 'קפוארה גן חובה',
            'walk_in': True, 'first_on': '2026-10-11', 'visits': 1,
        })
        self.assertEqual(found.family_id, child.family_id)
        self.assertEqual(found.child_ids, [str(child.id)])

    def test_with_no_present_mark_it_is_still_nobody(self):
        child = self.walk_in(YESTERDAY)
        LessonAttendance.objects.filter(child=child).update(status='absent')
        self.assertEqual(matching.match_phone(PHONE).outcome, 'not_found')

    def test_a_mark_of_a_day_still_ahead_is_no_visit(self):
        self.walk_in(TODAY + timedelta(days=2))
        self.assertEqual(matching.match_phone(PHONE).outcome, 'not_found')

    def test_the_phone_on_the_child_alone_is_enough(self):
        child = self.walk_in(YESTERDAY)
        Family.objects.filter(pk=child.family_id).update(phone='')
        self.assertTrue(matching.match_phone(PHONE).trial.get('walk_in'))

    def test_every_visit_counts_and_the_newest_names_the_day(self):
        child = self.walk_in(TODAY - timedelta(days=7))
        self.walk_in(TODAY)                       # the same child, the same lesson: the screen marks the row it has
        found = matching.match_phone(PHONE)
        self.assertEqual(found.child_ids, [str(child.id)])
        self.assertEqual(
            found.detail, 'הגיע 2 פעמים לשיעור קפוארה גן חובה (האחרונה ב-12.10) בלי להירשם · דני לוי · נוסף על ידי המדריך',
        )
        self.assertEqual((found.trial['on'], found.trial['first_on'], found.trial['visits']), ('2026-10-12', '2026-10-05', 2))

    def test_a_paying_child_on_the_phone_is_a_customer_not_a_walk_in(self):
        self.family_pays()
        self.walk_in(YESTERDAY, first='יונתן')
        self.assertIn(matching.match_phone(PHONE).outcome, ('customer_before', 'registered_after'))

    def test_a_trial_ahead_and_a_registration_begun_come_first(self):
        self.walk_in(YESTERDAY, first='יונתן')
        pending = self.real_child('pending')
        self.assertEqual(matching.match_phone(PHONE).outcome, 'pending')
        LessonEnrollment.objects.create(child=pending, lesson=self.lesson, status='active', trial_lesson_date=TODAY + timedelta(days=3))
        self.assertEqual(matching.match_phone(PHONE).outcome, 'trial_upcoming')

    def test_a_trial_gives_way_to_a_newer_walk_in_and_stands_before_an_older_one(self):
        self.walk_in(YESTERDAY, first='יונתן')
        tried = self.real_child('trial_completed')
        trial = LessonEnrollment.objects.create(
            child=tried, lesson=self.lesson, status='active', trial_lesson_date=TODAY - timedelta(days=5), trial_outcome='attended',
        )
        self.assertTrue(matching.match_phone(PHONE).trial.get('walk_in'))       # came again, unregistered, since the trial
        # The same day as the walk-in: the trial stands.
        LessonEnrollment.objects.filter(pk=trial.pk).update(trial_lesson_date=YESTERDAY, trial_held_on=YESTERDAY)
        found = matching.match_phone(PHONE)
        self.assertEqual(found.outcome, 'trial_only')
        self.assertNotIn('walk_in', found.trial)

    def test_said_in_words_of_its_own_to_the_screens_and_the_bot(self):
        walked = self.brought_in(YESTERDAY)
        self.assertEqual(matching.outcome_label(walked), WALK_IN_LABEL)
        self.assertEqual(matching.children_of(walked)[0]['status_label'], WALK_IN_LABEL)       # never "רפאים"
        card = shadow_tools.customer_card(ToolContext(contact=walked))
        self.assertEqual(card['outcome_label'], WALK_IN_LABEL)
        self.assertTrue(card['trial']['walk_in'])
        payload = self.get(f'contacts/{walked.id}/').data
        self.assertEqual(payload['kogo']['outcome_label'], WALK_IN_LABEL)
        self.assertEqual(payload['source_label'], WALK_IN_LABEL)


# --- the cron's look --------------------------------------------------------------------------------------

class FindTests(WalkInCase):
    def test_a_phone_that_never_wrote_becomes_a_lead_the_bot_comes_back_to(self):
        self.walk_in(TODAY)
        self.assertEqual(
            walk_ins.find(), {'walk_ins_seen': 1, 'walk_in_contacts_opened': 1, 'walk_in_contacts_matched': 1},
        )
        contact = self.found()
        self.assertEqual((contact.name, contact.source), ('משפחת לוי', 'kogo_walk_in'))
        self.assertTrue(matching.is_walk_in(contact))
        created = ContactEvent.objects.get(contact=contact, kind=EVENT_CREATED)
        self.assertEqual(created.text, 'נוסף מהנוכחות: דני לוי הגיע לשיעור קפוארה גן חובה ב-12.10 בלי להירשם, והמדריך רשם את הטלפון')
        self.assertEqual((contact.lead_stage, contact.lead_owner), ('trial_done', 'bot'))
        self.assertEqual(contact.lead_reason, 'הגיע לשיעור קפוארה גן חובה ב-12.10 בלי להירשם · דני לוי · נוסף על ידי המדריך.')
        row = self.open_row(contact)
        self.assertEqual((row.kind, row.step, row.template_key), ('trial_feedback', 1, 'trial_feedback_walk_in_1'))
        self.assertEqual(row.text, WALK_IN_TEXT)
        self.assertEqual(row.episode, 'trial_feedback|walk_in|2026-10-12')
        self.assertEqual(row.due_at, at(2026, 10, 13))                 # the next morning, when the office opens

    def test_looked_at_once_until_something_new_is_written_about_it(self):
        self.walk_in(TODAY)
        walk_ins.find()
        self.assertEqual(walk_ins.find()['walk_in_contacts_matched'], 0)
        self.clock.forward(days=7)
        self.walk_in(TODAY + timedelta(days=7))                        # came again a week later
        counts = walk_ins.find()
        self.assertEqual((counts['walk_in_contacts_opened'], counts['walk_in_contacts_matched']), (0, 1))
        self.assertEqual(Contact.objects.filter(phone=PHONE).count(), 1)
        self.assertEqual(self.found().kogo_trial['visits'], 2)
        # Still the one round of the walk-in, keyed by its first visit: nobody is greeted twice.
        self.assertEqual(set(LeadFollowUp.objects.values_list('episode', flat=True)), {'trial_feedback|walk_in|2026-10-12'})

    def test_a_phone_added_days_after_the_lesson_is_news(self):
        self.walk_in(TODAY, phone='')
        self.assertEqual(walk_ins.find()['walk_ins_seen'], 0)
        self.clock.forward(days=2)
        # The instructor fills the phone in afterwards — the screen's own call.
        set_ghost_contact(enrollment=LessonEnrollment.objects.get(ghost_visible_until__isnull=False), phone=LOCAL)
        self.assertEqual(walk_ins.find()['walk_in_contacts_opened'], 1)
        row = self.open_row(self.found())
        self.assertEqual(row.due_at, self.clock())                    # the morning after has passed: now, the office is open

    def test_a_phone_added_afterwards_that_already_wrote_is_matched_again(self):
        inbound.store_event(event=inbound.EVENT_CUSTOMER_MESSAGE, phone=PHONE, text='שלום, יש חוג קפוארה במינץ?')
        self.bot_reply('היי, יש! באיזה גיל?', phone=PHONE)
        self.walk_in(TODAY, phone='')
        self.clock.forward(hours=1)
        matching.recheck_contact(self.found())                         # the twelve-hourly match, before the phone was known
        self.assertEqual(self.found().kogo_outcome, 'not_found')
        self.clock.forward(hours=1)
        set_ghost_contact(enrollment=LessonEnrollment.objects.get(ghost_visible_until__isnull=False), phone=LOCAL)
        self.assertEqual(walk_ins.find()['walk_in_contacts_matched'], 1)
        self.assertTrue(matching.is_walk_in(self.found()))
        self.assertEqual(walk_ins.find()['walk_in_contacts_matched'], 0)          # and from then on, once is enough

    def test_a_customer_is_not_opened_as_a_lead(self):
        self.family_pays()
        self.walk_in(TODAY, first='יונתן')
        counts = walk_ins.find()
        self.assertEqual((counts['walk_ins_seen'], counts['walk_in_contacts_opened']), (1, 0))
        self.assertFalse(Contact.objects.filter(phone=PHONE).exists())

    def test_a_contact_who_wrote_before_is_matched_again(self):
        inbound.store_event(event=inbound.EVENT_CUSTOMER_MESSAGE, phone=PHONE, text='שלום, יש חוג קפוארה במינץ?')
        self.bot_reply('היי, יש! באיזה גיל?', phone=PHONE)
        Contact.objects.filter(phone=PHONE).update(kogo_outcome='not_found', kogo_checked_at=self.clock() - DAY)
        self.clock.forward(hours=1)
        self.walk_in(TODAY)
        counts = walk_ins.find()
        self.assertEqual((counts['walk_in_contacts_opened'], counts['walk_in_contacts_matched']), (0, 1))
        contact = self.found()
        self.assertEqual(contact.source, 'whatsapp')
        self.assertTrue(matching.is_walk_in(contact))
        self.assertEqual(contact.lead_stage, 'trial_done')

    def test_too_old_without_a_phone_or_not_a_mobile_is_not_looked_at(self):
        self.walk_in(TODAY - timedelta(days=15))
        self.walk_in(TODAY, phone='03-9123456', first='רוני')
        self.walk_in(TODAY, phone='', first='גלי')
        self.assertEqual(walk_ins.find()['walk_ins_seen'], 0)
        self.assertFalse(Contact.objects.exists())

    def test_the_cron_brings_them_in(self):
        self.walk_in(TODAY)
        counts = cron.tick()
        self.assertEqual(counts['walk_in_contacts_opened'], 1)
        self.assertEqual(self.found().lead_stage, 'trial_done')


# --- the bot's round ----------------------------------------------------------------------------------------

@override_settings(**SENDING_ON)
class RoundTests(WalkInCase):
    def test_its_own_template_first_then_the_trials_and_then_the_office(self):
        self.connect('trial_feedback_walk_in_1', 'trial_feedback_2', 'trial_feedback_3')
        contact = self.brought_in()
        self.play(at(2026, 10, 23, 12, 0))
        self.assert_sent(
            (at(2026, 10, 13), 'template', flow_of('trial_feedback_walk_in_1')),
            (at(2026, 10, 15), 'template', flow_of('trial_feedback_2')),
            (at(2026, 10, 19), 'template', flow_of('trial_feedback_3')),
        )
        first = LeadFollowUp.objects.filter(contact=contact).order_by('id').first()
        # What the conversation shows is what the parent got: the template's own words.
        self.assertEqual(first.message.text, WALK_IN_TEXT)
        contact = self.fresh(contact)
        self.assertEqual((contact.lead_stage, contact.lead_owner), ('trial_done', 'office'))

    def test_found_by_its_name_in_manychat(self):
        self.manychat.named('trial_feedback_walk_in_1')
        self.brought_in()
        self.play(at(2026, 10, 13, 11, 0))
        self.assert_sent((at(2026, 10, 13), 'template', 'auto_kogo_lead_walkin_1'))

    def test_without_its_template_nothing_leaves_and_the_screen_says_why(self):
        contact = self.brought_in()
        nxt = self.get(f'leads/{contact.id}/').data['next']
        self.assertEqual(
            (nxt['who'], nxt['what'], nxt['hold']), ('bot', 'הבוט יכתוב שראינו אותם בשיעור ויציע מקום קבוע', 'חסרה תבנית'),
        )
        self.play(at(2026, 10, 13, 11, 0))
        self.assertEqual(self.manychat.sent, [])
        row = self.open_row(contact)
        self.assertEqual(row.status, 'due')
        self.assertTrue(row.outcome_note.startswith('חסרה תבנית'))

    @override_settings(WAHUB_SENDING_ENABLED=False)
    def test_with_sending_off_nothing_leaves(self):
        self.connect('trial_feedback_walk_in_1')
        contact = self.brought_in()
        self.play(at(2026, 10, 13, 11, 0))
        self.assertEqual(self.manychat.sent, [])
        self.assertEqual(self.open_row(contact).outcome_note, 'השליחה ללקוחות כבויה')

    def test_with_the_follow_ups_switched_off_nothing_leaves(self):
        settings_store.WahubSetting.objects.filter(key=settings_store.BOT_FOLLOWUPS).delete()
        self.connect('trial_feedback_walk_in_1')
        contact = self.brought_in()
        self.play(at(2026, 10, 13, 11, 0))
        self.assertEqual(self.manychat.sent, [])
        self.assertEqual(self.open_row(contact).outcome_note, 'החזרות האוטומטיות כבויות')

    def test_registering_closes_the_lead_and_nothing_more_leaves(self):
        self.connect('trial_feedback_walk_in_1', 'trial_feedback_2')
        contact = self.brought_in()
        self.play(at(2026, 10, 13, 11, 0))
        self.assertEqual(len(self.manychat.sent), 1)
        self.family_pays(first='יונתן')                                 # the office registered the child, by the same phone
        self.play(at(2026, 10, 16, 12, 0))
        self.assertEqual(len(self.manychat.sent), 1)
        self.assertEqual(self.fresh(contact).lead_stage, 'registered')

    def test_the_parent_answering_is_the_offices_to_carry_on(self):
        self.connect('trial_feedback_walk_in_1', 'trial_feedback_2')
        contact = self.brought_in()
        self.play(at(2026, 10, 13, 11, 0))
        self.clock.forward(minutes=20)
        inbound.store_event(event=inbound.EVENT_CUSTOMER_MESSAGE, phone=PHONE, text='כן, נשמח לשריין')
        self.play(at(2026, 10, 16, 12, 0))
        self.assertEqual(len(self.manychat.sent), 1)
        self.assertEqual(self.fresh(contact).lead_owner, 'office')

    def test_somebody_who_asked_out_gets_nothing(self):
        self.connect('trial_feedback_walk_in_1')
        contact = self.brought_in()
        Contact.objects.filter(pk=contact.pk).update(opted_out={'at': self.clock().isoformat(), 'source': 'office'})
        lead_stage.refresh(contact)
        self.play(at(2026, 10, 13, 11, 0))
        self.assertEqual(self.manychat.sent, [])
        self.assertEqual(self.fresh(contact).lead_owner, 'office')

    def test_a_walk_in_too_long_ago_for_a_round_is_the_offices(self):
        self.walk_in(TODAY - timedelta(days=20))
        contact = Contact.objects.create(phone=PHONE, name='דנה')
        matching.recheck_contact(contact)
        contact = self.staged(contact)
        self.assertEqual((contact.lead_stage, contact.lead_owner), ('trial_done', 'office'))
        self.assertEqual(contact.lead_next_step, 'להתקשר: איך היה, ולהציע הרשמה')
        self.assertFalse(LeadFollowUp.objects.filter(contact=contact).exists())

    def test_a_walk_in_whose_tap_was_undone_gets_no_message_of_its_own(self):
        self.walk_in(TODAY)
        walk_ins.find()
        enrollment = LessonEnrollment.objects.get(ghost_visible_until__isnull=False)
        delete_ghost_enrollment(enrollment=enrollment)
        contact = self.found()
        matching.recheck_contact(contact)
        self.clock.forward(days=3)
        contact = self.staged(contact)
        self.assertEqual(contact.kogo_outcome, 'not_found')
        self.assertEqual((contact.lead_stage, contact.lead_owner), ('answered_silent', ''))
        self.assertFalse(LeadFollowUp.objects.filter(contact=contact, status__in=('planned', 'due')).exists())


# --- the screens and the words ------------------------------------------------------------------------------

class ScreenTests(WalkInCase):
    def test_the_lead_on_the_list_and_in_full(self):
        contact = self.brought_in()
        row = next(row for row in self.get('leads/', owner='bot').data['results'] if row['contact_id'] == contact.id)
        self.assertEqual(row['summary'], 'הגיע לשיעור קפוארה גן חובה ב-12.10 בלי להירשם · דני לוי · נוסף על ידי המדריך.')
        # The stage of a trial nobody registered after — said as what it was, on the lead and in the conversations.
        self.assertEqual((row['stage'], row['stage_label']), ('trial_done', WALK_IN_LABEL))
        self.assertEqual(self.get(f'contacts/{contact.id}/').data['lead']['stage_label'], WALK_IN_LABEL)
        details = self.get(f'leads/{contact.id}/').data
        child = LessonEnrollment.objects.get(ghost_visible_until__isnull=False).child
        self.assertEqual(details['details']['trial'], {
            'course': 'קפוארה גן חובה', 'branch': 'מינץ', 'date': '2026-10-12', 'instructor': 'יואב כהן', 'child': 'דני',
            'attended': True, 'walk_in': True, 'visits': 1, 'first_date': '2026-10-12',
        })
        self.assertEqual(details['details']['followups'][0]['kind_label'], WALK_IN_LABEL)
        self.assertEqual(details['details']['in_system']['outcome_label'], WALK_IN_LABEL)
        self.assertEqual(details['customer']['child_id'], str(child.id))

    def test_the_words_are_filed_with_the_others_and_are_the_templates_own(self):
        self.assertEqual(lead_stage.VARIANT_WALK_IN, lead_followup_seed.WALK_IN)
        self.assertEqual(lead_followup_seed.DEFAULTS['lead_trial_feedback_walk_in']['texts'], [WALK_IN_TEXT])
        self.assertEqual(lead_templates.FLOW_NAMES['trial_feedback_walk_in_1'], 'kogo_lead_walkin_1')
        lead_followup_seed.run()
        item = KnowledgeItem.objects.get(key='lead_trial_feedback_walk_in')
        self.assertEqual((item.body, item.is_active), (WALK_IN_TEXT, True))
        self.assertIn('kogo_lead_walkin_1', item.data['when'])
        row = lead_automation.template('trial_feedback_walk_in_1')
        self.assertEqual(
            (row['stage'], row['flow_name'], row['text'], row['required']), (2, 'kogo_lead_walkin_1', WALK_IN_TEXT, True),
        )
        self.assertEqual(row['when_label'], 'יום אחרי שהילד הגיע לשיעור בלי להירשם והמדריך רשם את הטלפון')
        # The trial's own first step still says it does not go to whoever got the system's message after the lesson.
        self.assertIn('לא יוצאת למי שכבר קיבל', lead_automation.template('trial_feedback_1')['when_label'])
