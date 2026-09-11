"""The calendar's per-event rental agreement download (scheduling/events/{id}/rental_agreement/).

It keeps working as it did, drawn now by the renderer of a tenancy's stored
contracts, from the terms of its one event.
"""
from datetime import date
from unittest import mock

from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.models import UserProfile
from apps.rentals.tests.factories import make_branch, make_rental, make_studio, make_user
from apps.scheduling.models import ScheduleEvent
from apps.scheduling.rental_agreement import generator
from apps.scheduling.rental_agreement.terms import TEMPLATE_VERSION, event_terms


def agreement_url(event):
    return f'/api/v1/scheduling/events/{event.id}/rental_agreement/'


class RentalAgreementDownloadTests(APITestCase):
    def setUp(self):
        self.branch = make_branch('פלורנטין')
        self.client.force_authenticate(make_user('manager-agreement@test', UserProfile.ROLE_MANAGER))

    def test_downloads_a_weekly_rentals_agreement(self):
        event = make_rental(
            self.branch, renter_name='דנה לוי', renter_id_number='123456782', price='120', days=(0, 2),
            studio=make_studio(self.branch),
        )
        res = self.client.get(agreement_url(event))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res['Content-Type'], 'application/pdf')
        self.assertEqual(res['Content-Disposition'], f'attachment; filename="rental-agreement-{event.id}.pdf"')
        self.assertTrue(res.content.startswith(b'%PDF'))

    def test_downloads_a_one_time_rentals_agreement(self):
        event = make_rental(self.branch, event_type='one_time', price='250')
        res = self.client.get(agreement_url(event))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.content.startswith(b'%PDF'))

    def test_draws_the_events_terms_with_the_contracts_renderer(self):
        event = make_rental(self.branch, price='120', days=(0, 2))
        with mock.patch.object(
            generator, 'generate_tenancy_contract_pdf', wraps=generator.generate_tenancy_contract_pdf,
        ) as render:
            res = self.client.get(agreement_url(event))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        # No version: nothing is stored, so no page names a contract version.
        render.assert_called_once_with(event_terms(ScheduleEvent.objects.get(pk=event.pk)))

    def test_the_terms_of_one_event(self):
        weekly = event_terms(make_rental(
            self.branch, renter_name=' דנה  לוי ', renter_id_number='123456782', price='120', days=(2, 0),
        ))
        self.assertEqual(weekly['template_version'], TEMPLATE_VERSION)
        self.assertEqual((weekly['tenant']['name'], weekly['tenant']['id_number']), ('דנה לוי', '123456782'))
        self.assertEqual([row['weekday'] for row in weekly['rows']], [0, 2])
        self.assertEqual([row['sum'] for row in weekly['rows']], ['480.00', '480.00'])
        # An event has no agreed amount of its own: its rows add up to it, as the calendar's PDF always said.
        self.assertEqual(
            (weekly['period'], weekly['monthly_amount'], weekly['vat_amount'], weekly['monthly_total']),
            ('monthly', '960.00', '172.80', '1132.80'),
        )

        once = event_terms(make_rental(self.branch, event_type='one_time', price='250'))
        self.assertEqual(once['period'], 'once')
        self.assertEqual([(row['kind'], row['date']) for row in once['rows']], [('one_time', '2026-09-06')])
        self.assertEqual((once['monthly_amount'], once['monthly_total']), ('250.00', '295.00'))

    def test_refuses_what_is_no_rental_or_has_no_dates(self):
        meeting = ScheduleEvent.objects.create(
            name='ישיבת צוות', event_date=date(2026, 9, 6), start_time='10:00', end_time='11:00', branch=self.branch,
        )
        res = self.client.get(agreement_url(meeting))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data, {'error': 'האירוע אינו שכירות סטודיו'})

        undated = make_rental(self.branch, contract=(None, None))
        res = self.client.get(agreement_url(undated))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data, {'error': 'חסרים תאריכי תוקף הסכם'})
