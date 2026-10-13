"""
The weekly audit (apps/core/system_audit.py) clicks through every read route as
a manager. The wahub routes have their day; the ones that send a message or run
the cron are never clicked, and clicking the rest changes nothing.
"""
import uuid

from django.utils import timezone

from apps.core.system_audit import all_routes, sweep_slice
from apps.wahub.models import Contact, ContactEvent, Message, Tag, WaitingListEntry
from apps.wahub.tests.base import WahubTestCase


class WeeklyAuditTests(WahubTestCase):
    def test_every_wahub_route_has_a_day(self):
        routes = [route for route in all_routes() if route.template.startswith('api/v1/wahub/')]
        self.assertGreaterEqual(len(routes), 20)
        self.assertEqual({route.area for route in routes}, {'messages'})

    def test_the_routes_that_send_or_run_a_job_are_never_called(self):
        by_template = {route.template: route for route in all_routes()}
        for template in (
            'api/v1/wahub/cron/tick/', 'api/v1/wahub/contacts/{pk}/send/', 'api/v1/wahub/contacts/{pk}/send-flow/',
        ):
            self.assertTrue(by_template[template].never_call, template)
        # The rest that change something answer POST, PATCH or PUT only: the sweep sends GET.
        for template in (
            'api/v1/wahub/inbound/manychat/', 'api/v1/wahub/settings/inbound-key/',
            'api/v1/wahub/contacts/{pk}/takeover/', 'api/v1/wahub/contacts/{pk}/release/',
            'api/v1/wahub/contacts/{pk}/read/', 'api/v1/wahub/contacts/{pk}/followup/',
            'api/v1/wahub/contacts/{pk}/tags/', 'api/v1/wahub/contacts/{pk}/needs-human/',
            'api/v1/wahub/contacts/{pk}/recheck/', 'api/v1/wahub/contacts/{pk}/analyze/',
            # The live test: the switch writes in ManyChat, the reply sends a real message.
            'api/v1/wahub/contacts/{pk}/live-test/', 'api/v1/wahub/live-test/reply/',
            # Leads, version 2: what a person does to a lead.
            'api/v1/wahub/leads/{pk}/close/', 'api/v1/wahub/leads/{pk}/reopen/',
            'api/v1/wahub/leads/{pk}/snooze/', 'api/v1/wahub/leads/{pk}/owner/',
            # Connecting a WhatsApp template to a follow-up answers PUT only.
            'api/v1/wahub/leads/templates/{template_key}/',
            # The ✓ and the clock of a conversation, and the waiting list: what a person does (POST only).
            'api/v1/wahub/contacts/{pk}/close/', 'api/v1/wahub/contacts/{pk}/hand-to-bot/', 'api/v1/wahub/contacts/{pk}/future/',
            'api/v1/wahub/contacts/{pk}/waiting-list/', 'api/v1/wahub/waiting-list/{pk}/handled/', 'api/v1/wahub/waiting-list/{pk}/remove/',
            # A note is rewritten by PATCH and deleted by DELETE.
            'api/v1/wahub/contacts/{pk}/notes/{note_id}/',
        ):
            self.assertFalse(by_template[template].callable_get, template)
        # Telling a customer that a seat came free answers GET too — what the press WOULD do, which reads only (the
        # sweep below opens it); the message itself leaves by POST alone.
        self.assertTrue(by_template['api/v1/wahub/waiting-list/{pk}/notify/'].callable_get)

    def test_the_sweep_reads_every_screen_and_leaves_everything_as_it_was(self):
        self.incoming('אפשר שיעור ניסיון בקפוארה?', name='רותם')
        self.incoming('נציג בבקשה')
        contact = self.contact()
        contact.tags.add(Tag.objects.create(name='חם'))
        # A row of a waiting list whose seat came free: the sweep opens "what would telling the customer do".
        waiting = WaitingListEntry.objects.create(
            contact=contact, lesson_id=uuid.uuid4(), course_name='קפוארה', status='freed', freed_at=timezone.now(),
        )
        before = (
            Contact.objects.values().get(), Message.objects.count(), ContactEvent.objects.count(),
            WaitingListEntry.objects.values().get(),
        )

        result = sweep_slice('messages', 0, budget_seconds=120)
        self.assertTrue(result.finished)
        ours = [outcome for outcome in result.outcomes if 'wahub' in outcome.path]
        called = {outcome.path: outcome for outcome in ours if not outcome.skipped}
        self.assertEqual(
            {path: outcome.status for path, outcome in called.items()},
            {
                'api/v1/wahub/contacts/': 200,
                'api/v1/wahub/contacts/counts/': 200,
                'api/v1/wahub/contacts/updates/': 200,
                f'api/v1/wahub/contacts/{contact.id}/': 200,
                f'api/v1/wahub/contacts/{contact.id}/messages/': 200,
                f'api/v1/wahub/contacts/{contact.id}/shadow/': 200,
                'api/v1/wahub/quick-replies/': 200,
                'api/v1/wahub/status/': 200,
                'api/v1/wahub/summary/': 200,
                'api/v1/wahub/tags/': 200,
                f'api/v1/wahub/tags/{Tag.objects.get().id}/': 200,
                # stage 2: the bot's knowledge, the shadow, the review and the demo list (all read only)
                'api/v1/wahub/knowledge/': 200,
                'api/v1/wahub/knowledge/from-kogo/': 200,
                'api/v1/wahub/knowledge/office-hours/now/': 200,
                'api/v1/wahub/shadow/summary/': 200,
                'api/v1/wahub/shadow/bad/': 200,
                'api/v1/wahub/shadow/recent/': 200,
                'api/v1/wahub/review/proposals/': 200,
                'api/v1/wahub/review/notes/': 200,
                'api/v1/wahub/review/summary/': 200,
                'api/v1/wahub/demo/scenarios/': 200,
                # stage 3: who asked and did not register; the card's block needs ?family= and, without it,
                # refuses in Hebrew (400) — the sweep fills path parameters only, and a refusal is not a fault.
                'api/v1/wahub/leads/unregistered/': 200,
                'api/v1/wahub/for-customer/': 400,
                # "דיוק" and the automatic approval: the list of threads and the switch, read only. What
                # writes there (a fix, say, undo, the switch itself) answers POST or PATCH only.
                'api/v1/wahub/refine/': 200,
                'api/v1/wahub/settings/review/': 200,
                # whom the new bot answers and whether its follow-ups may leave: reading the two switches changes
                # nothing (the moment a switch became live is set by the cron and the inbound door, never by a look).
                # Changing one answers POST only.
                'api/v1/wahub/settings/bot-mode/': 200,
                'api/v1/wahub/settings/followups/': 200,
                # the lead stages: the board is one aggregate, read only
                'api/v1/wahub/leads/board/': 200,
                # "בדיקות הבוט": the list of scripts with their last results, read only. Playing one — which asks
                # Claude and costs money — answers POST only, so the sweep never plays a script.
                'api/v1/wahub/bot/evals/': 200,
                # leads, version 2: the lists, one lead in full, the texts of the follow-ups — read only. What a
                # person does to a lead (close, reopen, snooze, owner) answers POST only.
                'api/v1/wahub/leads/': 200,
                'api/v1/wahub/leads/templates/': 200,
                f'api/v1/wahub/leads/{contact.id}/': 200,
                # "חזרה אוטומטית ללידים": what is missing for the follow-ups to leave by themselves, and the
                # automations ManyChat has to connect them to — read only (the list is ManyChat's own getFlows,
                # the one read the audit itself makes; with no key, as here, nobody is asked).
                'api/v1/wahub/leads/automation/': 200,
                'api/v1/wahub/leads/templates/options/': 200,
                # the notes about a customer and the waiting lists: read only. Writing a note, and whatever is done
                # to a row of a list, answers POST only.
                f'api/v1/wahub/contacts/{contact.id}/notes/': 200,
                'api/v1/wahub/waiting-list/': 200,
                # the lessons a person chooses from, and what telling a customer of a free seat would do now — both
                # read only. The telling itself answers POST only, so the sweep never sends it.
                'api/v1/wahub/waiting-list/lessons/': 200,
                f'api/v1/wahub/waiting-list/{waiting.id}/notify/': 200,
            },
        )
        self.assertEqual([outcome.error for outcome in ours if outcome.error], [])
        skipped = {outcome.path for outcome in ours if outcome.skipped}
        self.assertIn('api/v1/wahub/cron/tick/', skipped)
        # The routes that write (a demo scenario, a verdict, a note, an approval) answer POST or DELETE only.
        self.assertFalse({
            'api/v1/wahub/demo/scenario/', 'api/v1/wahub/demo/contacts/', 'api/v1/wahub/shadow/try/', 'api/v1/wahub/bot/evals/run/',
        } & set(called))

        self.assertEqual(
            (
                Contact.objects.values().get(), Message.objects.count(), ContactEvent.objects.count(),
                WaitingListEntry.objects.values().get(),
            ),
            before,
        )
        self.network.assert_not_called()
