"""
The weekly audit (apps/core/system_audit.py) clicks through every read route as
a manager. The wahub routes have their day; the ones that send a message or run
the cron are never clicked, and clicking the rest changes nothing.
"""
from apps.core.system_audit import all_routes, sweep_slice
from apps.wahub.models import Contact, ContactEvent, Message, Tag
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
        ):
            self.assertFalse(by_template[template].callable_get, template)

    def test_the_sweep_reads_every_screen_and_leaves_everything_as_it_was(self):
        self.incoming('אפשר שיעור ניסיון בקפוארה?', name='רותם')
        self.incoming('נציג בבקשה')
        contact = self.contact()
        contact.tags.add(Tag.objects.create(name='חם'))
        before = (
            Contact.objects.values().get(), Message.objects.count(), ContactEvent.objects.count(),
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
            },
        )
        self.assertEqual([outcome.error for outcome in ours if outcome.error], [])
        skipped = {outcome.path for outcome in ours if outcome.skipped}
        self.assertIn('api/v1/wahub/cron/tick/', skipped)
        # The routes that write (a demo scenario, a verdict, a note, an approval) answer POST or DELETE only.
        self.assertFalse({'api/v1/wahub/demo/scenario/', 'api/v1/wahub/demo/contacts/', 'api/v1/wahub/shadow/try/'} & set(called))

        self.assertEqual(
            (Contact.objects.values().get(), Message.objects.count(), ContactEvent.objects.count()), before,
        )
        self.network.assert_not_called()
