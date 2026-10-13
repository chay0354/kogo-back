"""
The bot coming back to a lead by itself (docs/WAHUB-CONTRACT-LEADS-V2.md, ג and ח.10):
when a follow-up is planned and for which moment, the office hours, Fridays,
Saturdays and special days, WhatsApp's 24 hours, what calls a follow-up off,
how a round ends — and above all what leaves and what does not: nothing while
the owner's switch is off, nothing twice, nothing ever for a demo contact.

ManyChat is a mock; `self.network` fails any test that reaches for the real one.
The clock is the test's (tests/test_leads_v2.py): Monday 12.10.2026, noon.
"""
from datetime import date, datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from apps.core.manychat_service import ManyChatError
from apps.wahub import cron, inbound, knowledge, lead_followup_seed, lead_followups, lead_stage, leads, settings_store
from apps.wahub.models import Contact, ContactEvent, KnowledgeItem, LeadFollowUp, Message
from apps.wahub.tests.test_leads_v2 import DAY, FOLLOWUP_MARKS, HOUR, MINUTE, TODAY, ZONE, LeadsCase, special_day

REQUEST = 'apps.core.manychat_service.ManyChatService._request'
OK = {'status': 'success'}
SENDING_ON = dict(MANYCHAT_KEY='test-key', WAHUB_SENDING_ENABLED=True)
LINK_FIRST = 'היי מה נשמע 🙂 רציתי לבדוק אם הצלחתם להירשם לקפוארה בראש העין או שמשהו נתקע בדרך'
LAST_TIME = 'היי פעם אחרונה שאני מזכירה 🙂 אם זה כבר לא רלוונטי תכתבו לי לא ואני מפסיקה. ואם כן אני כאן'


def at(year, month, day, hour=10, minute=30):
    return timezone.make_aware(datetime(year, month, day, hour, minute), ZONE)


def follow_ups_on(since='2026-01-01T00:00:00+00:00'):
    """The follow-ups' switch as it stands once the owner turned it on at `since` with sending on."""
    settings_store.put(settings_store.BOT_FOLLOWUPS, {'on': True, 'at': since, 'since': since, 'by': None, 'by_name': ''})


class EngineCase(LeadsCase):
    def setUp(self):
        super().setUp()
        # The registrations are read again before every send. These leads are invented on the contact
        # alone, so the reading is a mock here — and tried for real in its own test.
        recheck = patch.object(lead_followups.matching, 'recheck_contact', return_value=False)
        self.recheck = recheck.start()
        self.addCleanup(recheck.stop)
        # The owner's own switch for the follow-ups (bot_mode.py) is on here, and has been for long: these tests are
        # about the engine behind it. The switch itself — off until he turns it on, and what it lets go — is
        # tests/test_go_live.py.
        follow_ups_on()

    def link_lead(self, wrote=HOUR, **fields) -> Contact:
        """Somebody who got a registration link and did not register: the bot reminds them."""
        contact = self.lead(wrote, lead_link_sent_at=self.clock() - wrote, manychat_subscriber_id='4242', **fields)
        lead_stage.refresh(contact)
        return Contact.objects.get(pk=contact.pk)

    def trial_lead(self, **fields) -> Contact:
        """Did a trial yesterday, wrote last four days ago, did not register."""
        contact = self.lead(
            4 * DAY, kogo_outcome='trial_only', kogo_detail='ניסיון ב-11.10, הגיע · נועה לוי', manychat_subscriber_id='4242',
            kogo_trial={'on': (TODAY - timedelta(days=1)).isoformat(), 'outcome': 'attended', 'course': 'קפוארה גילאי 5–7'},
            **fields,
        )
        lead_stage.refresh(contact)
        return Contact.objects.get(pk=contact.pk)

    def rows(self, contact):
        return list(LeadFollowUp.objects.filter(contact=contact).order_by('id'))

    def open_row(self, contact) -> LeadFollowUp:
        return LeadFollowUp.objects.get(contact=contact, status__in=('planned', 'due'))

    def fresh(self, contact) -> Contact:
        return Contact.objects.get(pk=contact.pk)


# --- the office hours ----------------------------------------------------------------------------------------

class HoursTests(LeadsCase):
    def test_the_owners_hours_until_somebody_files_them(self):
        opening = lead_followups.next_opening
        self.assertEqual(opening(at(2026, 10, 12, 12, 0)), at(2026, 10, 12, 12, 0))         # Monday noon: open
        self.assertEqual(opening(at(2026, 10, 12, 9, 0)), at(2026, 10, 12))                 # before the office opens
        self.assertEqual(opening(at(2026, 10, 12, 18, 0)), at(2026, 10, 13))                # at closing time: tomorrow
        self.assertEqual(opening(at(2026, 10, 12, 23, 30)), at(2026, 10, 13))
        self.assertTrue(lead_followups.is_open(at(2026, 10, 12, 17, 59)))
        self.assertFalse(lead_followups.is_open(at(2026, 10, 12, 10, 29)))

    def test_never_on_a_friday_or_a_saturday(self):
        opening = lead_followups.next_opening
        self.assertEqual(opening(at(2026, 10, 15, 19, 0)), at(2026, 10, 18))                # Thursday evening → Sunday
        self.assertEqual(opening(at(2026, 10, 16, 11, 0)), at(2026, 10, 18))                # Friday
        self.assertEqual(opening(at(2026, 10, 17, 11, 0)), at(2026, 10, 18))                # Saturday
        # ... whatever the office hours say about Friday.
        week = {day: {'open': True, 'from': '09:00', 'to': '16:00'} for day in ('sun', 'mon', 'wed', 'thu', 'fri')}
        week.update(tue={'open': False}, sat={'open': False})
        knowledge.create_item({'kind': 'office_hours', 'title': 'שעות', 'weekly': week})
        self.assertEqual(opening(at(2026, 10, 16, 9, 30)), at(2026, 10, 18, 9, 0))
        self.assertEqual(opening(at(2026, 10, 12, 16, 30)), at(2026, 10, 14, 9, 0))         # Monday after 16:00; Tuesday closed
        self.assertEqual(opening(at(2026, 10, 12, 9, 30)), at(2026, 10, 12, 9, 30))         # the filed hours, not the default

    def test_not_on_a_special_day_unless_the_office_is_open_as_usual(self):
        special_day('חג', date(2026, 10, 13))
        special_day('ערב חג', date(2026, 10, 14), state_='hours')
        special_day('חול המועד', date(2026, 10, 15), state_='open')
        self.assertEqual(lead_followups.next_opening(at(2026, 10, 12, 19, 0)), at(2026, 10, 15))
        self.assertFalse(lead_followups.is_open(at(2026, 10, 13, 12, 0)))
        self.assertTrue(lead_followups.is_open(at(2026, 10, 15, 12, 0)))

    def test_when_a_step_is_due(self):
        due = lead_followups.due_at
        now = self.clock()
        asked = at(2026, 10, 12, 11, 1)
        self.assertEqual(due(asked, DAY, now), at(2026, 10, 13))                            # a day later, when the office opens
        self.assertEqual(due(asked, 3 * DAY, now), at(2026, 10, 15))
        self.assertEqual(due(asked, 7 * DAY, now), at(2026, 10, 19))
        self.assertEqual(due(at(2026, 10, 15, 12, 0), DAY, now), at(2026, 10, 18))          # Thursday + a day is Friday → Sunday
        # The day itself: when the office opens on it — or now, when that has passed.
        self.assertEqual(due(at(2027, 3, 14, 12, 0), timedelta(0), now), at(2027, 3, 14))
        self.assertEqual(due(at(2026, 10, 12, 12, 0), timedelta(0), now), now)
        # Less than a day: that long after the fact, inside the hours.
        self.assertEqual(due(at(2026, 10, 12, 11, 50), 2 * HOUR, now), at(2026, 10, 12, 13, 50))
        self.assertEqual(due(at(2026, 10, 12, 17, 30), 2 * HOUR, at(2026, 10, 12, 17, 30)), at(2026, 10, 13))
        # Never before now, and never sooner than two days after the step before it.
        self.assertEqual(due(at(2026, 10, 1, 12, 0), DAY, now), now)
        self.assertEqual(due(at(2026, 10, 1, 12, 0), 3 * DAY, now, previous=now), at(2026, 10, 14, 12, 0))


# --- the words -----------------------------------------------------------------------------------------------

class WordsTests(LeadsCase):
    def test_a_phrasing_names_only_what_is_known(self):
        render = lead_followups.render
        first = lead_followup_seed.DEFAULTS['lead_link_reminder']['texts'][0]
        self.assertEqual(render(first, {'חוג': 'קפוארה', 'סניף': 'ראש העין'}), LINK_FIRST)
        self.assertEqual(render(first, {'חוג': 'קפוארה'}), 'היי מה נשמע 🙂 רציתי לבדוק אם הצלחתם להירשם לקפוארה או שמשהו נתקע בדרך')
        self.assertEqual(render(first, {}), 'היי מה נשמע 🙂 רציתי לבדוק אם הצלחתם להירשם לחוג או שמשהו נתקע בדרך')
        feedback = lead_followup_seed.DEFAULTS['lead_trial_feedback']['texts'][0]
        self.assertEqual(render(feedback, {'ילד': 'נועה'}), 'היי איך היה לנועה בשיעור הניסיון? 🙂')
        self.assertEqual(render(feedback, {'ילד': ''}), 'היי איך היה בשיעור הניסיון? 🙂')                 # no name is invented
        check = lead_followup_seed.DEFAULTS['lead_will_check']['texts'][0]
        self.assertEqual(render(check, {}), 'היי רק מזכירה לגבי החוג 🙂 יש כבר החלטה או שנשארה שאלה?')
        self.assertEqual(render(check, {'חוג': 'קפוארה', 'ילד': 'נועה'}), 'היי רק מזכירה לגבי קפוארה לנועה 🙂 יש כבר החלטה או שנשארה שאלה?')
        self.assertEqual(render('שיעורי ניסיון[ ב{חוג}] גם במועדים אחרים', {}), 'שיעורי ניסיון גם במועדים אחרים')
        self.assertEqual(render('שיעור ב{חוג} מ{חוג}', {}), 'שיעור בחוג מהחוג')
        self.assertEqual(render('שורה {משהו} אחת\n  ושנייה {קישור} ', {'קישור': 'https://x.example/'}), 'שורה אחת\nושנייה https://x.example/')

    def test_every_draft_is_short_asks_one_thing_and_has_one_emoji_at_most(self):
        emojis = '🙂🎈🙏😊👋'
        for key, draft in lead_followup_seed.DEFAULTS.items():
            for text in draft['texts']:
                with self.subTest(key=key, text=text[:30]):
                    filled = lead_followups.render(text, {'חוג': 'קפוארה', 'סניף': 'ראש העין', 'ילד': 'נועה', 'גיל': 3, 'קישור': 'https://x.example/'})
                    self.assertLessEqual(len(filled.splitlines()), 3)
                    self.assertLessEqual(filled.count('?'), 2)
                    self.assertLessEqual(sum(filled.count(emoji) for emoji in emojis), 1)
                    self.assertNotIn('{', filled)
                    self.assertNotIn('[', filled)
                    for guess in ('תוכל ', 'תוכלי ', 'אתה ', '₪', 'הנחה', 'נשאר מקום'):       # no guessed gender, no price in a fixed text
                        self.assertNotIn(guess, filled)
                    # The owner's word (12.10): the way a person writes on WhatsApp — "היי" with no comma after
                    # it, no "כאן קוגומלו", no sales talk, no formal punctuation.
                    self.assertTrue(filled.startswith('היי'), filled)
                    for formal in ('היי,', 'כאן קוגומלו', 'אשמח', 'נשמח', '—', ';'):
                        self.assertNotIn(formal, filled)

    def test_the_drafts_are_filed_once_and_the_owners_edits_are_what_is_sent(self):
        self.assertEqual(lead_followups.phrasing('link_reminder', 1)[1], 'link_reminder_1')
        filed_all = len(lead_followup_seed.PHRASINGS)
        nothing_revised = {'revised': 0, 'revision_left': [], 'reworded': 0}
        self.assertEqual(lead_followup_seed.run(), {'created': filed_all, 'skipped': 0, **nothing_revised})
        self.assertEqual(lead_followup_seed.run(), {'created': 0, 'skipped': filed_all, **nothing_revised})
        filed = KnowledgeItem.objects.filter(kind='phrasing', key__startswith='lead_')
        self.assertEqual(filed.count(), len(lead_followup_seed.PHRASINGS))
        self.assertEqual(set(filed.filter(is_active=False).values_list('key', flat=True)), {'lead_dormant'})
        item = filed.get(key='lead_will_check')
        self.assertEqual(item.when_to_say, 'internal')
        self.assertIn('משתנים', item.data['when'])

        knowledge.update_item(item, {'body': 'היי, יש חדש לגבי {חוג}?'}, self.manager)
        self.assertEqual(lead_followups.phrasing('will_check', 1), ('היי, יש חדש לגבי {חוג}?', 'will_check_1'))
        self.assertEqual(lead_followup_seed.run()['created'], 0)                 # filing again never undoes his edit
        self.assertEqual(KnowledgeItem.objects.get(pk=item.pk).body, 'היי, יש חדש לגבי {חוג}?')
        # A phrasing he turned off is not sent, and the draft does not come back in its place.
        knowledge.soft_delete(item, self.manager)
        self.assertEqual(lead_followups.phrasing('will_check', 1), ('', ''))
        self.assertEqual(lead_followup_seed.run()['created'], 0)

    def test_a_variant_has_its_own_first_words_and_the_kinds_for_the_rest(self):
        self.assertEqual(lead_followups.phrasing('link_reminder', 1, 'signup')[1], 'link_reminder_signup_1')
        self.assertIn('שהתחלתם להירשם', lead_followups.phrasing('link_reminder', 1, 'signup')[0])
        self.assertEqual(lead_followups.phrasing('link_reminder', 2, 'signup'), lead_followups.phrasing('link_reminder', 2))
        self.assertEqual(lead_followups.phrasing('link_reminder', 3, 'commitment')[1], 'link_reminder_3')
        self.assertEqual(lead_followups.phrasing('date_arrived', 2, 'age')[1], 'date_arrived_age_2')
        self.assertEqual(lead_followups.phrasing('will_check', 2), ('', ''))                  # one step only
        self.assertEqual(lead_followups.phrasing('dormant', 1), ('', ''))                     # a draft, off until he decides

    def test_the_bot_that_answers_customers_never_reads_these_phrasings(self):
        lead_followup_seed.run()
        knowledge.create_item({'kind': 'phrasing', 'key': 'greeting', 'title': 'פתיחה', 'body': 'היי, כאן דנה'})
        keys = {item.key for item in knowledge.relevant_items(today=TODAY) if item.kind == 'phrasing'}
        self.assertEqual(keys, {'greeting'})
        listed = self.get('knowledge/', kind='phrasing').data
        self.assertEqual(len(listed), len(lead_followup_seed.PHRASINGS) + 1)           # he edits them on the same screen

    def test_the_deploy_command_files_them_with_the_old_bots_texts(self):
        out = StringIO()
        call_command('wahub_import_old_knowledge', stdout=out)
        self.assertIn(f'נוסחי החזרה ללידים: נוצר {len(lead_followup_seed.PHRASINGS)}; 0 כבר היו.', out.getvalue())
        again = StringIO()
        call_command('wahub_import_old_knowledge', stdout=again)
        self.assertIn(f'נוסחי החזרה ללידים: נוצר 0; {len(lead_followup_seed.PHRASINGS)} כבר היו.', again.getvalue())


# --- planning, cancelling, and how a round ends ---------------------------------------------------------------

class RoundTests(EngineCase):
    def test_entering_a_stage_of_the_bots_plans_its_first_step(self):
        contact = self.link_lead()
        (row,) = self.rows(contact)
        self.assertEqual((row.kind, row.step, row.status), ('link_reminder', 1, 'planned'))
        self.assertEqual((row.due_at, row.next_look_at), (at(2026, 10, 13), at(2026, 10, 13)))     # tomorrow, when the office opens
        self.assertEqual((row.text, row.template_key), (LINK_FIRST, 'link_reminder_1'))
        self.assertEqual(row.why, contact.lead_reason)
        self.assertEqual(row.episode, lead_stage.compute(contact)['follow']['key'])
        round_ = contact.lead_bot
        self.assertEqual((round_['episode'], round_['step'], round_['steps'], round_['attempts'], round_['done']), (row.episode, 1, 3, 0, False))
        # Planned once: another look plans nothing more, and leaves the contact alone.
        self.assertEqual(lead_stage.pending_count(), 0)
        lead_stage.refresh(contact)
        lead_stage.scan()
        self.assertEqual(len(self.rows(contact)), 1)
        self.assertEqual(Message.objects.count(), 0)

    def test_a_stage_with_nothing_for_the_bot_to_say_plans_nothing(self):
        for fields in (dict(wrote=5 * DAY), dict(wrote=HOUR), dict(kogo_outcome='trial_upcoming'), dict(kogo_outcome='registered_after')):
            contact = self.staged(self.lead(**fields))
            self.assertEqual(self.rows(contact), [])
            self.assertEqual(contact.lead_bot, {})

    def test_three_times_a_day_three_days_and_a_week_and_then_the_lead_goes_dormant(self):
        contact = self.link_lead(is_demo=True, source='demo')            # a demo: every step is written, nothing leaves
        for step, (moment, text) in enumerate((
            (at(2026, 10, 13), LINK_FIRST),
            (at(2026, 10, 15), 'היי רק מזכירה 🙂 אם בא לכם לנסות שיעור אחד בקפוארה ולראות אם זה מתאים תכתבו לי ואשלח קישור'),
            (at(2026, 10, 19), LAST_TIME),
        ), 1):
            row = self.open_row(contact)
            self.assertEqual((row.step, row.due_at, row.text, row.template_key), (step, moment, text, f'link_reminder_{step}'))
            self.clock.moment = moment - MINUTE
            self.assertEqual(lead_followups.run()['followups_sent'], 0)          # not a minute early
            self.clock.moment = moment
            self.assertEqual(lead_followups.run()['followups_sent'], 1)
        self.assertEqual([row.status for row in self.rows(contact)], ['sent', 'sent', 'sent'])       # and no fourth
        after = self.fresh(contact)
        self.assertEqual((after.lead_bot['attempts'], after.lead_bot['done']), (3, True))
        # The last word is the bot's; the customer has three days to answer it.
        self.assertEqual((after.lead_stage, after.lead_owner), ('link_sent', 'bot'))
        self.assertIn('הבוט חזר אליו 3 פעמים', after.lead_reason)
        self.assertEqual(leads.next_move(after, None)['what'], 'הבוט חזר אליו — מחכים לתשובה')
        self.clock.forward(days=3)
        lead_stage.scan()
        dormant = self.fresh(contact)
        self.assertEqual((dormant.lead_stage, dormant.lead_owner), ('cold', ''))
        self.assertEqual(dormant.lead_reason, 'שאל על קפוארה בראש העין ב-12.10. הבוט חזר אליו 3 פעמים ולא נענה. לא נרשם.')
        self.assertEqual(self.rows(contact)[-1].status, 'sent')
        self.assertEqual(LeadFollowUp.objects.filter(contact=contact, status__in=('planned', 'due')).count(), 0)

    def test_a_hot_lead_whose_round_is_over_waits_for_the_office(self):
        contact = self.trial_lead(is_demo=True, source='demo')
        row = self.open_row(contact)
        self.assertEqual((row.kind, row.step, row.text), ('trial_feedback', 1, 'היי איך היה בשיעור הניסיון? 🙂'))
        self.assertEqual(row.due_at, self.clock())                       # the day after the trial is today, and the office is open
        for moment in (self.clock(), at(2026, 10, 14, 12, 0), at(2026, 10, 18)):
            self.assertEqual(self.open_row(contact).due_at, moment)
            self.clock.moment = moment
            lead_followups.run()
        texts = [row.text for row in self.rows(contact)]
        self.assertNotIn('http', texts[0])                               # first "how was it" — never a link to pay
        self.assertIn('אפשר להירשם כאן\n🔗 https://cogomelo.co.il/', texts[1])
        after = self.fresh(contact)
        self.assertEqual((after.lead_stage, after.lead_owner), ('trial_done', 'bot'))
        self.clock.forward(days=3)
        lead_stage.scan()
        office = self.fresh(contact)
        self.assertEqual((office.lead_stage, office.lead_owner), ('trial_done', 'office'))
        self.assertIn('הבוט חזר אליו 3 פעמים ולא נענה', office.lead_reason)
        self.assertEqual(office.lead_next_step, 'להתקשר: איך היה, ולהציע הרשמה')
        self.assertEqual(ContactEvent.objects.filter(contact=contact, kind='lead_stage_changed').latest('id').text,
                         'עשה ניסיון ולא נרשם · הצעד הבא: הבוט ← המשרד')

    def test_a_hot_lead_who_answers_the_bot_is_the_offices_from_there(self):
        contact = self.trial_lead(is_demo=True, source='demo')
        lead_followups.run()
        self.assertEqual(self.open_row(contact).step, 2)
        self.clock.forward(hours=2)
        inbound.store_event(event=inbound.EVENT_CUSTOMER_MESSAGE, phone=contact.phone, text='היה נחמד, נחשוב על זה')
        after = self.fresh(contact)
        self.assertEqual(after.lead_owner, 'office')
        second = self.rows(contact)[-1]
        self.assertEqual((second.step, second.status, second.outcome_note), (2, 'skipped', 'הלקוח כתב בינתיים — עבר לטיפול המשרד'))

    def test_will_check_is_asked_once_and_then_it_is_a_phone_call(self):
        contact = self.staged(self.lead(is_demo=True, source='demo', said={'blocker': 'ask_someone', 'blocker_text': 'אמרה שתבדוק עם בעלה'}))
        row = self.open_row(contact)
        self.assertEqual((row.kind, row.step, row.due_at), ('will_check', 1, at(2026, 10, 13)))
        self.assertEqual(row.text, 'היי רק מזכירה לגבי קפוארה 🙂 יש כבר החלטה או שנשארה שאלה?')
        self.clock.moment = row.due_at
        lead_followups.run()
        self.assertEqual([row.status for row in self.rows(contact)], ['sent'])               # once: no second step
        self.assertEqual(self.fresh(contact).lead_owner, 'bot')
        self.clock.forward(days=3)
        lead_stage.scan()
        after = self.fresh(contact)
        self.assertEqual((after.lead_stage, after.lead_owner, after.lead_next_step), ('will_check', 'office', 'להתקשר למי שמחליט'))

    def test_a_child_too_young_is_written_to_on_the_day_and_a_week_later(self):
        too_young = {
            'blocker': 'too_young', 'come_back': {'kind': 'age', 'years': 2, 'months': 7, 'said_on': '2026-10-12'},
            'come_back_on': '2027-03-12', 'come_back_why': 'הילד יהיה בן 3',
        }
        contact = self.staged(self.lead(is_demo=True, source='demo', said=too_young, known_callback_on=date(2027, 3, 12)))
        row = self.open_row(contact)
        self.assertEqual((row.kind, row.step, row.template_key), ('date_arrived', 1, 'date_arrived_age_1'))
        self.assertEqual(row.due_at, at(2027, 3, 14))                    # 12.3.2027 is a Friday: Sunday morning
        self.assertEqual(row.text, 'היי 🙂 שאלתם אותנו פעם על חוג והילד היה עוד קטן מדי. עכשיו כבר אפשר, מתחילים מגיל 3. רוצים שאבדוק לכם קבוצה בראש העין?')
        # Until that day: nothing, whatever runs.
        self.clock.to(2026, 12, 1, 11)
        lead_stage.scan()
        self.assertEqual(lead_followups.run()['followups_sent'], 0)
        self.assertEqual(self.fresh(contact).lead_stage, 'waiting_date')
        self.clock.moment = at(2027, 3, 14)
        lead_stage.scan()
        self.assertEqual(self.fresh(contact).lead_stage, 'callback_due')
        self.assertEqual(lead_followups.run()['followups_sent'], 1)
        second = self.open_row(contact)
        self.assertEqual((second.step, second.due_at, second.template_key), (2, at(2027, 3, 21), 'date_arrived_age_2'))
        self.clock.moment = second.due_at
        lead_stage.scan()
        lead_followups.run()
        self.assertEqual([row.status for row in self.rows(contact)], ['sent', 'sent'])       # two, not three

    def test_somebody_who_wrote_nothing_of_their_own_is_asked_once_what_they_look_for(self):
        contact = self.staged(self.lead(10 * MINUTE, is_demo=True, source='demo', said={'empty_opening': True}))
        self.assertEqual((contact.lead_stage, contact.lead_owner), ('in_conversation', 'bot'))
        row = self.open_row(contact)
        self.assertEqual((row.kind, row.due_at), ('opening', at(2026, 10, 12, 13, 50)))       # two hours after their message
        self.assertEqual(row.text, 'היי 🙂 ראיתי שהתעניינתם בחוגים שלנו. לאיזה גיל ובאיזו עיר?')
        self.clock.moment = row.due_at
        lead_followups.run()
        after = self.fresh(contact)
        self.assertEqual([row.status for row in self.rows(contact)], ['sent'])
        self.assertEqual((after.lead_stage, after.lead_owner), ('in_conversation', ''))

    def test_the_customer_writing_calls_the_follow_up_off_and_a_new_round_begins_when_the_talk_is_over(self):
        contact = self.link_lead()
        self.clock.forward(hours=2)
        inbound.store_event(event=inbound.EVENT_CUSTOMER_MESSAGE, phone=contact.phone, text='תודה, ומה המחיר?')
        (row,) = self.rows(contact)
        self.assertEqual((row.status, row.outcome_note, row.acted_at), ('skipped', 'הלקוח כתב בינתיים', self.clock()))
        self.assertEqual(self.fresh(contact).lead_stage, 'in_conversation')
        self.clock.forward(minutes=1)
        inbound.store_event(event=inbound.EVENT_BOT_REPLY, phone=contact.phone, text='260 ₪ לחודש')
        again = self.open_row(contact)
        self.assertEqual((again.step, again.due_at), (1, at(2026, 10, 13)))                  # from the first step, a day after the talk
        self.assertNotEqual(again.episode, row.episode)
        self.assertEqual(self.fresh(contact).lead_bot['attempts'], 0)

    def test_a_person_taking_the_lead_or_the_conversation_calls_it_off(self):
        taken = self.link_lead()
        leads.set_owner(taken.id, 'office', self.manager)
        self.assertEqual((self.rows(taken)[0].status, self.rows(taken)[0].outcome_note), ('skipped', 'עבר לטיפול המשרד'))
        answered = self.link_lead()
        Contact.objects.filter(pk=answered.pk).update(handled_by='human')
        lead_stage.refresh(answered)
        self.assertEqual(self.rows(answered)[0].status, 'skipped')
        closed = self.link_lead()
        leads.close(closed.id, 'not_interested', '', self.manager)
        self.assertEqual(self.rows(closed)[0].outcome_note, 'הליד נסגר: לא מעוניין')

    def test_with_no_live_words_nothing_is_planned_and_nobody_asks_again(self):
        knowledge.create_item({'kind': 'phrasing', 'key': 'lead_link_reminder', 'title': 'כבוי', 'body': 'x', 'is_active': False})
        contact = self.link_lead()
        self.assertEqual(self.rows(contact), [])
        self.assertEqual((contact.lead_bot['done'], contact.lead_bot['note']), (True, 'אין נוסח פעיל לחזרה הזאת'))
        lead_stage.refresh(contact)
        self.assertEqual(self.rows(contact), [])
        self.assertEqual(self.fresh(contact).lead_owner, 'bot')
        # A hot lead with nothing to say to it is the office's.
        knowledge.create_item({'kind': 'phrasing', 'key': 'lead_trial_feedback', 'title': 'כבוי', 'body': 'x', 'is_active': False})
        self.assertEqual(self.trial_lead().lead_owner, 'office')

    def test_only_one_follow_up_is_ever_open_for_a_contact(self):
        contact = self.link_lead()
        with self.assertRaises(IntegrityError), transaction.atomic():
            LeadFollowUp.objects.create(
                contact=contact, kind='will_check', step=1, episode='x', due_at=self.clock(), next_look_at=self.clock(),
            )
        # A closed one beside it is fine: the history of a lead is many rows.
        LeadFollowUp.objects.create(
            contact=contact, kind='will_check', step=1, episode='x', due_at=self.clock(), next_look_at=self.clock(), status='skipped',
        )

    def test_nothing_a_person_marked_is_written_by_the_follow_ups(self):
        contact = self.link_lead(
            is_demo=True, source='demo', followup_status='no_answer', followup_note='לא ענתה', followup_by=self.manager,
            followup_at=self.clock() - 5 * DAY,
        )
        marks = Contact.objects.values(*FOLLOWUP_MARKS, 'lead_manual').get(pk=contact.pk)
        self.clock.moment = at(2026, 10, 13)
        self.assertEqual(lead_followups.run()['followups_sent'], 1)
        lead_stage.scan()
        self.assertEqual(Contact.objects.values(*FOLLOWUP_MARKS, 'lead_manual').get(pk=contact.pk), marks)


# --- what leaves, and what does not --------------------------------------------------------------------------

class SendingTests(EngineCase):
    def test_while_the_switch_is_off_nothing_leaves_and_the_follow_up_says_it_was_due(self):
        contact = self.link_lead()
        self.clock.moment = at(2026, 10, 13)
        with patch(REQUEST) as request:
            counts = lead_followups.run()
        request.assert_not_called()
        self.network.assert_not_called()
        self.assertEqual((counts['followups_due'], counts['followups_sent']), (1, 0))
        row = self.open_row(contact)
        self.assertEqual((row.status, row.outcome_note, row.acted_at), ('due', 'השליחה ללקוחות כבויה', None))
        self.assertEqual(Message.objects.count(), 0)                     # not even a line in the conversation
        self.assertEqual(self.fresh(contact).lead_bot['attempts'], 0)
        move = self.get(f'leads/{contact.id}/').data['next']
        self.assertEqual((move['who'], move['state'], move['note']), ('bot', 'due', 'השליחה ללקוחות כבויה'))
        # Tried again every half hour, quietly.
        self.clock.forward(minutes=5)
        self.assertEqual(lead_followups.run()['followups_due'], 0)
        self.clock.forward(minutes=30)
        self.assertEqual(lead_followups.run()['followups_due'], 1)
        self.assertEqual(ContactEvent.objects.filter(contact=contact, kind='lead_followup').count(), 0)

    def test_a_follow_up_that_could_not_leave_for_two_days_is_let_go(self):
        contact = self.link_lead()
        self.clock.moment = at(2026, 10, 13)
        lead_followups.run()
        self.clock.moment = at(2026, 10, 15)
        self.assertEqual(lead_followups.run()['followups_skipped'], 1)
        first, second = self.rows(contact)
        self.assertEqual((first.status, first.outcome_note), ('skipped', 'לא נשלח: השליחה ללקוחות כבויה'))
        # The round goes on — so turning the switch on never releases a pile of old reminders.
        self.assertEqual((second.step, second.status, second.due_at), (2, 'planned', at(2026, 10, 18)))
        self.assertEqual(self.fresh(contact).lead_bot['attempts'], 0)
        self.assertEqual(ContactEvent.objects.filter(contact=contact, kind='lead_followup').latest('id').text,
                         'קיבל קישור ועוד לא נרשם, פנייה 1: לא נשלח — השליחה ללקוחות כבויה')

    def test_a_hot_lead_the_bot_could_not_write_to_goes_to_the_office_instead_of_waiting_a_week(self):
        contact = self.trial_lead()
        lead_followups.run()
        self.assertEqual(self.open_row(contact).status, 'due')
        self.assertEqual(self.fresh(contact).lead_owner, 'bot')
        self.clock.forward(days=2)
        lead_followups.run()
        after = self.fresh(contact)
        self.assertEqual([row.status for row in self.rows(contact)], ['skipped'])            # no second step
        self.assertEqual((after.lead_stage, after.lead_owner), ('trial_done', 'office'))
        self.assertIn('הבוט לא חזר אליו (השליחה ללקוחות כבויה)', after.lead_reason)

    @override_settings(**SENDING_ON)
    def test_inside_whatsapps_24_hours_the_words_go_out_as_they_were_written(self):
        contact = self.link_lead()                                       # wrote at 11:00; tomorrow 10:30 is inside the window
        self.clock.moment = at(2026, 10, 13)
        with patch(REQUEST, return_value=OK) as request:
            counts = lead_followups.run()
        request.assert_called_once_with('POST', '/fb/sending/sendContent', json_body={
            'subscriber_id': 4242,
            'data': {'version': 'v2', 'content': {'type': 'whatsapp', 'messages': [{'type': 'text', 'text': LINK_FIRST}]}},
        })
        self.assertEqual(counts['followups_sent'], 1)
        first, second = self.rows(contact)
        self.assertEqual((first.status, first.acted_at, first.outcome_note), ('sent', self.clock(), ''))
        message = first.message
        self.assertEqual((message.direction, message.sender, message.new_bot, message.status, message.text), ('out', 'bot', True, 'sent', LINK_FIRST))
        self.assertEqual((second.step, second.status, second.due_at), (2, 'planned', at(2026, 10, 15)))
        after = self.fresh(contact)
        self.assertEqual((after.lead_bot['attempts'], after.lead_stage, after.lead_owner), (1, 'link_sent', 'bot'))
        self.assertEqual(ContactEvent.objects.filter(contact=contact, kind='lead_followup').latest('id').text,
                         f'קיבל קישור ועוד לא נרשם, פנייה 1: {LINK_FIRST}')

    @override_settings(**SENDING_ON)
    def test_after_24_hours_only_a_mapped_template_leaves(self):
        contact = self.link_lead(3 * HOUR)                               # wrote at 9:00; tomorrow 10:30 is outside the window
        self.clock.moment = at(2026, 10, 13)
        with patch(REQUEST, return_value=OK) as request:
            counts = lead_followups.run()
        # ManyChat is asked once which automations it has — it has none under this follow-up's name — and nothing is sent.
        request.assert_called_once_with('GET', '/fb/page/getFlows', timeout=8)
        row = self.open_row(contact)
        self.assertEqual((counts['followups_due'], row.status), (1, 'due'))
        self.assertIn('תבנית מאושרת', row.outcome_note)
        self.assertEqual(Message.objects.count(), 0)

        # The owner made the template and it was connected: now it leaves, as that template.
        settings_store.put(settings_store.LEAD_FOLLOWUP_TEMPLATES, {'link_reminder_1': 'content20261012120000_111111'})
        self.clock.forward(minutes=30)
        with patch(REQUEST, return_value=OK) as request:
            self.assertEqual(lead_followups.run()['followups_sent'], 1)
        request.assert_called_once_with('POST', '/fb/sending/sendFlow', json_body={
            'subscriber_id': 4242, 'flow_ns': 'content20261012120000_111111',
        })
        sent = self.rows(contact)[0]
        self.assertEqual((sent.status, sent.message.message_type, sent.message.text, sent.message.new_bot), ('sent', 'template', LINK_FIRST, True))
        # The second step has no template yet: when its day comes it will stay due.
        self.assertEqual(self.rows(contact)[1].template_key, 'link_reminder_2')

    @override_settings(**SENDING_ON)
    def test_a_send_manychat_refused_is_not_tried_again(self):
        contact = self.link_lead()
        self.clock.moment = at(2026, 10, 13)
        refusal = ManyChatError('Validation error', status_code=400, payload={'status': 'error', 'message': 'Validation error'})
        with patch(REQUEST, side_effect=refusal) as request:
            self.assertEqual(lead_followups.run()['followups_failed'], 1)
            self.clock.forward(minutes=40)
            lead_followups.run()
        self.assertEqual(request.call_count, 1)
        first, second = self.rows(contact)
        self.assertEqual(first.status, 'failed')
        self.assertTrue(first.outcome_note)
        self.assertEqual(first.message.status, 'failed')
        self.assertEqual((second.step, second.status), (2, 'planned'))
        self.assertEqual(self.fresh(contact).lead_bot['attempts'], 0)    # nothing reached the customer

    @override_settings(**SENDING_ON)
    def test_a_demo_contact_never_gets_anything_even_with_the_switch_on(self):
        contact = self.link_lead(is_demo=True, source='demo')
        self.clock.moment = at(2026, 10, 13)
        with patch(REQUEST, return_value=OK) as request:
            self.assertEqual(lead_followups.run()['followups_sent'], 1)
        request.assert_not_called()
        self.network.assert_not_called()
        row = self.rows(contact)[0]
        self.assertEqual((row.status, row.outcome_note), ('sent', 'נכתב לשיחת הדמו — לא נשלח לאף אחד'))
        # Written into the demo conversation as the new bot's line, so the round can be watched.
        self.assertEqual((row.message.sender, row.message.new_bot, row.message.status, row.message.text), ('bot', True, 'simulated', LINK_FIRST))
        self.assertEqual(self.fresh(contact).last_message_text, LINK_FIRST)

    def test_a_demo_contact_is_written_to_its_conversation_while_the_switch_is_off_too(self):
        contact = self.link_lead(is_demo=True, source='demo')
        self.clock.moment = at(2026, 10, 13)
        self.assertEqual(lead_followups.run()['followups_sent'], 1)
        self.assertEqual(Message.objects.get(contact=contact).status, 'simulated')
        self.network.assert_not_called()

    @override_settings(MANYCHAT_KEY='test-key')
    def test_the_owners_own_number_under_the_live_test_gets_it_while_the_switch_is_off(self):
        contact = self.link_lead(live_test=True)
        other = self.link_lead()
        self.clock.moment = at(2026, 10, 13)
        with patch(REQUEST, return_value=OK) as request:
            counts = lead_followups.run()
        self.assertEqual((counts['followups_sent'], counts['followups_due']), (1, 1))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.rows(contact)[0].status, 'sent')
        self.assertEqual(self.rows(other)[0].status, 'due')

    @override_settings(**SENDING_ON)
    def test_the_registrations_are_read_again_before_a_send_and_a_lead_who_registered_gets_nothing(self):
        contact = self.link_lead()

        def registered(row):
            Contact.objects.filter(pk=row.pk).update(kogo_outcome='registered_after', kogo_detail='נרשם ב-12.10 · נועה לוי')
            return True

        self.recheck.side_effect = registered
        self.clock.moment = at(2026, 10, 13)
        with patch(REQUEST, return_value=OK) as request:
            self.assertEqual(lead_followups.run()['followups_skipped'], 1)
        request.assert_not_called()
        self.recheck.assert_called_once()
        self.assertEqual(self.recheck.call_args.args[0].pk, contact.pk)
        (row,) = self.rows(contact)
        self.assertEqual((row.status, row.outcome_note), ('skipped', 'נרשם'))
        self.assertEqual(self.fresh(contact).lead_stage, 'registered')

    @override_settings(**SENDING_ON)
    def test_if_the_registrations_cannot_be_read_nothing_is_sent_and_it_is_tried_again(self):
        contact = self.link_lead()
        self.recheck.side_effect = RuntimeError('database')
        self.clock.moment = at(2026, 10, 13)
        with patch(REQUEST, return_value=OK) as request:
            lead_followups.run()
        request.assert_not_called()
        row = self.open_row(contact)
        self.assertEqual((row.status, row.claimed_at, row.next_look_at), ('planned', None, self.clock() + timedelta(minutes=30)))

    @override_settings(**SENDING_ON)
    def test_a_moment_outside_the_office_hours_waits_for_the_next_opening(self):
        contact = self.link_lead()
        late = at(2026, 10, 13, 20, 0)
        LeadFollowUp.objects.filter(contact=contact).update(due_at=late, next_look_at=late)
        special_day('חג', date(2026, 10, 14))
        self.clock.moment = late
        with patch(REQUEST, return_value=OK) as request:
            lead_followups.run()
            row = self.open_row(contact)
            self.assertEqual((row.status, row.next_look_at, row.claimed_at), ('planned', at(2026, 10, 15), None))   # not on the holiday either
            self.clock.moment = at(2026, 10, 14, 12, 0)
            lead_followups.run()
            request.assert_not_called()
            self.clock.moment = at(2026, 10, 15)
            settings_store.put(settings_store.LEAD_FOLLOWUP_TEMPLATES, {'link_reminder_1': 'flow-1'})
            self.assertEqual(lead_followups.run()['followups_sent'], 1)
        self.assertEqual(request.call_count, 1)

    @override_settings(**SENDING_ON)
    def test_two_runs_never_send_one_follow_up_twice(self):
        contact = self.link_lead()
        self.clock.moment = at(2026, 10, 13)
        with patch(REQUEST, return_value=OK) as request:
            lead_followups.run()
            lead_followups.run()
            cron.tick()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(Message.objects.filter(contact=contact).count(), 1)

    @override_settings(**SENDING_ON)
    def test_a_row_another_call_is_acting_on_is_left_to_that_call(self):
        contact = self.link_lead()
        self.clock.moment = at(2026, 10, 13)
        LeadFollowUp.objects.filter(contact=contact).update(claimed_at=self.clock())
        with patch(REQUEST, return_value=OK) as request:
            self.assertEqual(lead_followups.run(), {'followups_sent': 0, 'followups_due': 0, 'followups_skipped': 0, 'followups_failed': 0})
            lead_stage.refresh(contact)                                   # nor does a look at the stage touch it
        request.assert_not_called()
        self.assertEqual(self.open_row(contact).status, 'planned')
        self.assertIsNone(lead_followups._claim(self.open_row(contact).pk, self.clock()))

    @override_settings(**SENDING_ON)
    def test_a_send_that_was_cut_in_the_middle_is_closed_and_never_repeated(self):
        contact = self.link_lead()
        self.clock.moment = at(2026, 10, 13)
        LeadFollowUp.objects.filter(contact=contact).update(claimed_at=self.clock() - timedelta(minutes=11))
        with patch(REQUEST, return_value=OK) as request:
            self.assertEqual(lead_followups.run()['followups_failed'], 1)
        request.assert_not_called()
        first = self.rows(contact)[0]
        self.assertEqual(first.status, 'failed')
        self.assertIn('לא ידוע אם ההודעה יצאה', first.outcome_note)

    def test_a_follow_up_that_raises_does_not_stop_the_others(self):
        broken, fine = self.link_lead(is_demo=True, source='demo'), self.link_lead(is_demo=True, source='demo')
        self.clock.moment = at(2026, 10, 13)
        real = lead_followups._act

        def act(row, now, **kwargs):
            if row.contact_id == broken.id:
                raise RuntimeError('boom')
            return real(row, now, **kwargs)

        with patch.object(lead_followups, '_act', side_effect=act), self.assertLogs('apps.wahub.lead_followups', level='ERROR'):
            self.assertEqual(lead_followups.run()['followups_sent'], 1)
        self.assertEqual(self.rows(fine)[0].status, 'sent')
        self.assertIsNotNone(self.rows(broken)[0].claimed_at)           # held: whatever happened, it is not sent again

    def test_a_run_with_no_time_does_nothing(self):
        contact = self.link_lead(is_demo=True, source='demo')
        self.clock.moment = at(2026, 10, 13)
        self.assertEqual(lead_followups.run(budget_seconds=0)['followups_sent'], 0)
        self.assertEqual(self.open_row(contact).status, 'planned')

    def test_the_five_minute_slice_runs_the_follow_ups_after_the_stages(self):
        contact = self.lead(HOUR, lead_link_sent_at=self.clock() - HOUR)          # not looked at yet
        counts = cron.tick()
        self.assertEqual(self.open_row(contact).status, 'planned')               # the stage, and with it the plan
        self.assertEqual((counts['followups_sent'], counts['followups_due']), (0, 0))
        self.clock.moment = at(2026, 10, 13)
        counts = cron.tick()
        self.assertEqual((counts['followups_due'], counts['followups_sent'], counts['pending_lead_stage']), (1, 0, 0))
        self.network.assert_not_called()
        with patch.object(lead_followups, 'run', side_effect=RuntimeError('boom')), self.assertLogs('apps.wahub.cron', level='ERROR'):
            self.assertIn('seconds', cron.tick())                                 # a fault here does not stop the slice


class DormantTests(EngineCase):
    def test_dormant_leads_are_written_to_once_on_the_day_the_owner_set_and_not_before_he_set_one(self):
        cold = self.staged(self.lead(40 * DAY, is_demo=True, source='demo'))
        long_gone = self.staged(self.lead(500 * DAY, is_demo=True, source='demo'))
        open_lead = self.link_lead(is_demo=True, source='demo')
        self.assertEqual(cold.lead_stage, 'cold')
        self.assertEqual(lead_followups.plan_dormant(self.clock()), 0)           # no day set
        settings_store.put(settings_store.LEAD_DORMANT_ON, TODAY.isoformat())
        self.assertEqual(lead_followups.plan_dormant(self.clock()), 0)           # a day, and no live words: still nothing
        knowledge.create_item({'kind': 'phrasing', 'key': 'lead_dormant', 'title': 'נפתחה הרשמה', 'body': 'היי, נפתחה ההרשמה 🙂 רוצים לשמוע מה חדש?'})
        self.assertEqual(lead_followups.run()['followups_sent'], 1)
        (row,) = self.rows(cold)
        self.assertEqual((row.kind, row.status, row.text), ('dormant', 'sent', 'היי, נפתחה ההרשמה 🙂 רוצים לשמוע מה חדש?'))
        self.assertEqual(self.rows(long_gone), [])                               # wrote more than a year ago
        self.assertEqual([row.kind for row in self.rows(open_lead)], ['link_reminder'])
        # Once: not again tomorrow, nor a week after the day.
        self.clock.forward(days=1)
        self.assertEqual(lead_followups.plan_dormant(self.clock()), 0)
        settings_store.put(settings_store.LEAD_DORMANT_ON, (TODAY - timedelta(days=30)).isoformat())
        self.assertEqual(lead_followups.plan_dormant(self.clock()), 0)
