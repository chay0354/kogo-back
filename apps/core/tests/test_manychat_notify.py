"""ManyChat registration WhatsApp: custom fields must land before the template."""
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from apps.core.manychat_service import ManyChatError, ManyChatService


class CardLinkFlowTests(SimpleTestCase):
    """
    The office's card link sends through a WhatsApp template, not free text.

    The owner's rule (14.9): a parent is asked for a card only when the card on
    file stopped working, so one template — the card-update one — covers both the
    charge that failed and the link the office sends. An automation of its own is
    still honoured if one is ever created.
    """

    def test_the_setting_is_declared_so_the_environment_reaches_it(self):
        # It was read but never declared, so setting it in Vercel changed nothing
        # and every card link went out as free text.
        self.assertTrue(hasattr(settings, 'MANYCHAT_CARD_LINK_FLOW_NS'))

    @override_settings(MANYCHAT_CARD_LINK_FLOW_NS='content_card_link')
    def test_the_configured_flow_is_used(self):
        svc = ManyChatService(api_key='x')
        svc.get_flows = MagicMock()
        self.assertEqual(svc.resolve_flow_ns('MANYCHAT_CARD_LINK_FLOW_NS'), 'content_card_link')
        svc.get_flows.assert_not_called()

    @override_settings(MANYCHAT_CARD_LINK_FLOW_NS='')
    def test_without_one_an_automation_named_card_link_is_found(self):
        svc = ManyChatService(api_key='x')
        svc.get_flows = MagicMock(return_value=[
            {'name': 'card-update', 'ns': 'content_update'},
            {'name': 'card-link', 'ns': 'content_link'},
        ])
        self.assertEqual(svc.resolve_flow_ns('MANYCHAT_CARD_LINK_FLOW_NS'), 'content_link')

    @override_settings(MANYCHAT_CARD_LINK_FLOW_NS='')
    def test_the_name_lookup_alone_never_borrows_another_automation(self):
        # Name matching stays strict: 'card-update' is not a card-link automation.
        svc = ManyChatService(api_key='x')
        svc.get_flows = MagicMock(return_value=[{'name': 'card-update', 'ns': 'content_update'}])
        self.assertEqual(svc.resolve_flow_ns('MANYCHAT_CARD_LINK_FLOW_NS'), '')

    @override_settings(MANYCHAT_CARD_LINK_FLOW_NS='', MANYCHAT_CARD_UPDATE_FLOW_NS='')
    def test_with_no_card_link_automation_the_send_uses_the_card_update_one(self):
        # One template for both, by the owner's rule — and never free text, which
        # WhatsApp delivers only inside the 24-hour window.
        svc = ManyChatService(api_key='x')
        svc.get_flows = MagicMock(return_value=[{'name': 'card-update', 'ns': 'content_update'}])
        entry = svc._REGISTRATION_KINDS[ManyChatService.REGISTRATION_KIND_CARD_LINK]
        self.assertEqual(svc.resolve_flow_for(entry), 'content_update')

    @override_settings(MANYCHAT_CARD_LINK_FLOW_NS='', MANYCHAT_CARD_UPDATE_FLOW_NS='')
    def test_an_automation_of_its_own_still_wins(self):
        svc = ManyChatService(api_key='x')
        svc.get_flows = MagicMock(return_value=[
            {'name': 'card-update', 'ns': 'content_update'},
            {'name': 'card-link', 'ns': 'content_link'},
        ])
        entry = svc._REGISTRATION_KINDS[ManyChatService.REGISTRATION_KIND_CARD_LINK]
        self.assertEqual(svc.resolve_flow_for(entry), 'content_link')


class AvailableAutomationsTests(SimpleTestCase):
    def test_a_manychat_failure_is_not_masqueraded_as_an_empty_picker(self):
        svc = ManyChatService(api_key='x')
        svc.get_flows = MagicMock(side_effect=ManyChatError('ManyChat unavailable'))

        with self.assertRaisesRegex(ManyChatError, 'ManyChat unavailable'):
            svc.list_available_automations()


class RentalFlowTests(SimpleTestCase):
    """
    The two automations the studio tenants send through, resolved by their
    exact ManyChat names: 'rental-contract' and 'rental-card-update'.

    A tenant is a merchant, not a parent, so neither may ever borrow one of the
    courses' templates: those name a child and a course.
    """

    KINDS = (
        ('MANYCHAT_RENTAL_CONTRACT_FLOW_NS', 'rental-contract',
         ManyChatService.REGISTRATION_KIND_RENTAL_CONTRACT),
        ('MANYCHAT_RENTAL_CARD_UPDATE_FLOW_NS', 'rental-card-update',
         ManyChatService.REGISTRATION_KIND_RENTAL_CARD_UPDATE),
    )

    def test_the_settings_are_declared_so_the_environment_reaches_them(self):
        # Declaring them is what lets the owner pin a flow ns in Vercel; the
        # card-link setting was read for weeks without being declared.
        for setting, _name, _kind in self.KINDS:
            self.assertTrue(hasattr(settings, setting), setting)

    def test_the_configured_flow_ns_wins_without_asking_manychat(self):
        for setting, _name, _kind in self.KINDS:
            with self.subTest(setting=setting), override_settings(**{setting: 'content_pinned'}):
                svc = ManyChatService(api_key='x')
                svc.get_flows = MagicMock()
                self.assertEqual(svc.resolve_flow_ns(setting), 'content_pinned')
                svc.get_flows.assert_not_called()

    def test_without_one_the_automation_is_found_by_its_exact_name(self):
        flows = [
            {'name': 'card-update', 'ns': 'content_courses_update'},
            {'name': 'card-link', 'ns': 'content_courses_link'},
            {'name': 'rental-contract', 'ns': 'content_rental_contract'},
            {'name': 'rental-card-update', 'ns': 'content_rental_card'},
        ]
        for setting, name, _kind in self.KINDS:
            with self.subTest(name=name), override_settings(**{setting: ''}):
                svc = ManyChatService(api_key='x')
                svc.get_flows = MagicMock(return_value=flows)
                expected = next(f['ns'] for f in flows if f['name'] == name)
                self.assertEqual(svc.resolve_flow_ns(setting), expected)

    @override_settings(MANYCHAT_RENTAL_CONTRACT_FLOW_NS='', MANYCHAT_RENTAL_CARD_UPDATE_FLOW_NS='',
                       MANYCHAT_CARD_UPDATE_FLOW_NS='', MANYCHAT_CARD_LINK_FLOW_NS='')
    def test_a_tenant_send_never_borrows_a_courses_automation(self):
        # The card-link kind falls back to card-update on purpose. A tenant's
        # kinds must not: 'החיוב החודשי עבור ילד בחוג' to a merchant is wrong.
        svc = ManyChatService(api_key='x')
        svc.get_flows = MagicMock(return_value=[
            {'name': 'card-update', 'ns': 'content_courses_update'},
            {'name': 'card-link', 'ns': 'content_courses_link'},
        ])
        for _setting, _name, kind in self.KINDS:
            with self.subTest(kind=kind):
                entry = svc._REGISTRATION_KINDS[kind]
                self.assertNotIn('fallback_flow_setting', entry)
                self.assertEqual(svc.resolve_flow_for(entry), '')

    def test_both_kinds_are_listed_for_the_office_with_a_hebrew_label(self):
        for _setting, _name, kind in self.KINDS:
            self.assertIn(kind, ManyChatService._REGISTRATION_KINDS)
            self.assertTrue(ManyChatService.AUTOMATION_LABELS.get(kind))


class FallbackTextTests(SimpleTestCase):
    """
    The free text a send drops to when no automation exists. WhatsApp only
    delivers it inside the 24-hour window, but when it does it must read like
    a message and carry the link — never raise on the way out.
    """

    def _svc(self):
        svc = ManyChatService(api_key='x')
        svc.lookup_or_create = MagicMock(return_value={'subscriber_id': 7})
        svc.get_subscriber = MagicMock(return_value={'whatsapp_phone': '972501234567'})
        svc.set_custom_fields = MagicMock(return_value={'status': 'success'})
        svc.resolve_flow_ns = MagicMock(return_value='')
        svc.send_whatsapp_text = MagicMock(return_value={'status': 'success'})
        return svc

    def _send(self, svc, kind, extra_fields):
        return svc.notify_registration(
            phone='0501234567', parent_name='סטודיו אור', child_name='סטודיו אור',
            course_name='שכירות סטודיו', day_name='', start_time='', end_time='',
            branch_name='פלורנטין', kind=kind, extra_fields=extra_fields,
        )

    def test_the_contract_text_carries_the_signing_link_and_the_branch(self):
        svc = self._svc()
        result = self._send(
            svc, ManyChatService.REGISTRATION_KIND_RENTAL_CONTRACT,
            {'kogo_rental_sign_url': 'https://kogo.example/s/abc123', 'kogo_amount': '1,456.78'},
        )
        self.assertEqual(result['method'], 'text')
        text = svc.send_whatsapp_text.call_args[0][1]
        self.assertIn('https://kogo.example/s/abc123', text)
        self.assertIn('סטודיו אור', text)
        self.assertIn('בסניף פלורנטין', text)

    def test_the_card_text_carries_the_card_link_and_the_amount(self):
        svc = self._svc()
        result = self._send(
            svc, ManyChatService.REGISTRATION_KIND_RENTAL_CARD_UPDATE,
            {'kogo_card_update_url': 'https://kogo.example/rc/tok', 'kogo_amount': '1456.78'},
        )
        self.assertEqual(result['method'], 'text')
        text = svc.send_whatsapp_text.call_args[0][1]
        self.assertIn('https://kogo.example/rc/tok', text)
        self.assertIn('1456.78', text)

    def test_a_placeholder_nobody_filled_reads_as_nothing_instead_of_crashing(self):
        # {course_suffix} in the card-link template had no value passed to
        # format(); the fallback raised KeyError mid-send instead of sending.
        svc = self._svc()
        result = svc.notify_registration(
            phone='0501234567', parent_name='הורה', child_name='ילד', course_name='קפוארה',
            day_name='ראשון', start_time='18:00', end_time='19:00', branch_name='פלורנטין',
            kind=ManyChatService.REGISTRATION_KIND_CARD_LINK,
            extra_fields={'kogo_card_update_url': 'https://kogo.example/c/tok', 'kogo_amount': '250'},
        )
        self.assertTrue(result['sent'])
        self.assertEqual(result['method'], 'text')
        self.assertIn('https://kogo.example/c/tok', svc.send_whatsapp_text.call_args[0][1])


class SetCustomFieldsFallbackTests(SimpleTestCase):
    def test_retries_fields_one_by_one_when_batch_fails(self):
        svc = ManyChatService(api_key='x')
        calls = []

        def fake_request(method, path, **kwargs):
            if path.endswith('getCustomFields'):
                return {'status': 'success', 'data': []}
            calls.append(kwargs.get('json_body'))
            body = kwargs.get('json_body') or {}
            fields = body.get('fields') or []
            if len(fields) > 1:
                raise ManyChatError('unknown field kogo_location')
            if fields and fields[0].get('field_name') == 'kogo_location':
                raise ManyChatError('unknown field')
            return {'status': 'success'}

        svc._request = fake_request
        result = svc.set_custom_fields(11, {
            'kogo_branch_name': 'יהודה הלוי',
            'kogo_location': 'missing-in-manychat',
        })
        self.assertTrue(result.get('partial'))
        self.assertEqual(result.get('applied'), 1)
        self.assertEqual(len(calls), 3)

    def test_raises_when_every_field_fails(self):
        svc = ManyChatService(api_key='x')
        svc._request = MagicMock(side_effect=ManyChatError('boom'))
        with self.assertRaises(ManyChatError):
            svc.set_custom_fields(11, {'kogo_branch_name': 'יהודה הלוי'})

    def test_also_writes_timestamp_alias_fields(self):
        svc = ManyChatService(api_key='x')
        posted = []

        def fake_request(method, path, **kwargs):
            if path.endswith('getCustomFields'):
                return {
                    'status': 'success',
                    'data': [
                        {'id': 1, 'name': 'kogo_branch_name', 'type': 'text'},
                        {'id': 2, 'name': 'kogo_branch_name (2026-07-26 07:28:15)', 'type': 'text'},
                    ],
                }
            posted.append(kwargs.get('json_body'))
            return {'status': 'success'}

        svc._request = fake_request
        svc.set_custom_fields(11, {'kogo_branch_name': 'מינץ 24'})
        names = {row['field_name'] for row in posted[0]['fields']}
        self.assertIn('kogo_branch_name', names)
        self.assertIn('kogo_branch_name (2026-07-26 07:28:15)', names)

    def test_skips_fields_that_do_not_exist_in_manychat(self):
        svc = ManyChatService(api_key='x')
        posted = []

        def fake_request(method, path, **kwargs):
            if path.endswith('getCustomFields'):
                return {
                    'status': 'success',
                    'data': [{'id': 1, 'name': 'kogo_branch_name', 'type': 'text'}],
                }
            posted.append(kwargs.get('json_body'))
            return {'status': 'success'}

        svc._request = fake_request
        svc.set_custom_fields(11, {
            'kogo_branch_name': 'מינץ 24',
            'kogo_location': 'does-not-exist',
        })
        names = {row['field_name'] for row in posted[0]['fields']}
        self.assertEqual(names, {'kogo_branch_name'})


class NotifyRegistrationFieldOrderTests(SimpleTestCase):
    def _svc(self):
        svc = ManyChatService(api_key='x')
        svc.lookup_or_create = MagicMock(return_value={'subscriber_id': 99})
        svc.get_subscriber = MagicMock(return_value={'whatsapp_phone': '972501234567'})
        svc.resolve_flow_ns = MagicMock(return_value='content123_flow')
        svc.send_flow = MagicMock(return_value={'status': 'success'})
        svc.send_whatsapp_text = MagicMock(return_value={'status': 'success'})
        return svc

    def _kwargs(self, **overrides):
        payload = {
            'phone': '0501234567',
            'parent_name': 'הורה בדיקה',
            'child_name': 'ילד בדיקה',
            'course_name': 'קפוארה',
            'day_name': 'ראשון',
            'start_time': '18:15',
            'end_time': '19:00',
            'branch_name': 'יהודה הלוי',
            'kind': ManyChatService.REGISTRATION_KIND_TRIAL,
            'trial_date': '06/09/2026',
            'location': 'רחוב יהודה הלוי 10',
        }
        payload.update(overrides)
        return payload

    @patch('apps.core.manychat_service.time.sleep')
    def test_writes_fields_waits_then_sends_flow(self, mock_sleep):
        svc = self._svc()
        svc.set_custom_fields = MagicMock(return_value={'status': 'success'})

        result = svc.notify_registration(**self._kwargs())

        self.assertEqual(result['method'], 'flow')
        fields = svc.set_custom_fields.call_args[0][1]
        self.assertEqual(fields['kogo_branch_name'], 'יהודה הלוי')
        self.assertEqual(fields['kogo_trial_date'], '06/09/2026')
        self.assertEqual(fields['kogo_lesson_time'], '18:15-19:00')
        self.assertEqual(fields['kogo_location'], 'רחוב יהודה הלוי 10')
        mock_sleep.assert_called_once()

    @patch('apps.core.manychat_service.time.sleep')
    def test_writes_extra_card_update_fields(self, mock_sleep):
        svc = self._svc()
        svc.set_custom_fields = MagicMock(return_value={'status': 'success'})

        result = svc.notify_registration(
            **self._kwargs(
                kind=ManyChatService.REGISTRATION_KIND_CARD_UPDATE,
                extra_fields={
                    'kogo_card_update_url': 'https://kogo-front.vercel.app/update-card/tok',
                    'kogo_card_update_token': 'tok',
                    'kogo_amount': '250',
                },
            )
        )

        self.assertEqual(result['method'], 'flow')
        fields = svc.set_custom_fields.call_args[0][1]
        self.assertEqual(fields['kogo_amount'], '250')
        self.assertEqual(fields['kogo_card_update_token'], 'tok')
        self.assertIn('update-card/tok', fields['kogo_card_update_url'])
        svc.send_flow.assert_called_once_with(99, 'content123_flow')
        svc.send_whatsapp_text.assert_not_called()

    @patch('apps.core.manychat_service.time.sleep')
    def test_skips_empty_template_when_fields_fail(self, mock_sleep):
        svc = self._svc()
        svc.set_custom_fields = MagicMock(side_effect=ManyChatError('set failed'))

        result = svc.notify_registration(**self._kwargs())

        self.assertTrue(result['sent'])
        self.assertEqual(result['method'], 'text')
        svc.send_flow.assert_not_called()
        svc.send_whatsapp_text.assert_called_once()
        mock_sleep.assert_not_called()
        text = svc.send_whatsapp_text.call_args[0][1]
        self.assertIn('יהודה הלוי', text)
        self.assertIn('18:15-19:00', text)


class AutomationListTests(SimpleTestCase):
    """
    The list the office picks from on the customers page.

    The owner's report (15.9): "כל הטמפלטים של הווצאפ זה מציע רק חלק לשליחת
    הודעה". A template Kogo knows is a template the office may pick; whether
    ManyChat has an automation by that name is a fact to report, not a reason
    to drop the row.
    """

    def _svc(self):
        svc = ManyChatService(api_key='x')
        svc.resolve_flow_ns = MagicMock(return_value='')
        return svc

    def test_every_kogo_template_is_listed_even_when_manychat_answers_with_nothing(self):
        svc = self._svc()
        svc.get_flows = MagicMock(return_value=[])
        payload = svc.automations_payload()
        listed = {row['automation_id'] for row in payload['automations']}
        self.assertEqual(listed, set(ManyChatService._REGISTRATION_KINDS))
        self.assertTrue(payload['manychat_ok'])
        self.assertEqual(payload['manychat_count'], 0)
        self.assertTrue(all(row['in_manychat'] is False for row in payload['automations']))

    def test_a_list_that_never_came_back_says_so(self):
        svc = self._svc()
        svc.get_flows = MagicMock(side_effect=ManyChatError('boom'))
        payload = svc.automations_payload()
        self.assertFalse(payload['manychat_ok'])
        self.assertEqual(payload['manychat_count'], 0)
        # Still the whole set, so the screen is never half a list with no reason.
        self.assertEqual(
            {row['automation_id'] for row in payload['automations']},
            set(ManyChatService._REGISTRATION_KINDS),
        )

    def test_an_automation_manychat_has_is_listed_once_and_marked(self):
        svc = ManyChatService(api_key='x')
        svc.resolve_flow_ns = MagicMock(return_value='')
        svc.get_flows = MagicMock(return_value=[{'ns': 'ns_card', 'name': 'card-update'}])
        payload = svc.automations_payload()
        rows = [r for r in payload['automations'] if r['automation_id'] == 'card_update']
        self.assertEqual(len(rows), 1, 'a kind ManyChat has must not be listed twice')
        self.assertEqual(rows[0]['flow_ns'], 'ns_card')
        self.assertTrue(rows[0]['in_manychat'])
        self.assertEqual(payload['manychat_count'], 1)

    def test_a_flow_that_is_not_a_kogo_template_is_listed_too(self):
        svc = self._svc()
        svc.get_flows = MagicMock(return_value=[{'ns': 'ns_x', 'name': 'ברכת יום הולדת'}])
        rows = svc.automations_payload()['automations']
        theirs = [r for r in rows if r['automation_id'] == 'ns_x']
        self.assertEqual(len(theirs), 1)
        self.assertEqual(theirs[0]['automation_type'], 'flow')
        self.assertEqual(theirs[0]['label'], 'ברכת יום הולדת')

    def test_the_old_entry_point_still_returns_a_plain_list(self):
        svc = self._svc()
        svc.get_flows = MagicMock(return_value=[])
        self.assertIsInstance(svc.list_available_automations(), list)
