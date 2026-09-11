"""An ordinary edit of a calendar slot never writes its tenancy link: a link or unlink made while the edit was open stays."""
from decimal import Decimal

from django.test import TestCase

from apps.rentals.models import Tenancy
from apps.rentals.tests.factories import make_branch, make_customer, make_rental, make_studio
from apps.scheduling.event_serializers import ScheduleEventSerializer
from apps.scheduling.models import ScheduleEvent


class EventEditKeepsLinkTests(TestCase):
    def setUp(self):
        self.branch = make_branch('פלורנטין')
        self.tenancy = Tenancy.objects.create(
            tenant=make_customer(), branch=self.branch, monthly_amount=Decimal('400'),
        )
        self.slot = make_rental(
            self.branch, renter_id_number='123456789', studio=make_studio(self.branch), city=self.branch.city,
        )

    def edit_saved_after(self, concurrent_tenancy):
        """Load the slot for an edit, let the tenancy endpoints change its link, then save the edit."""
        serializer = ScheduleEventSerializer(
            ScheduleEvent.objects.get(pk=self.slot.pk), data={'name': 'שכירות ערב'}, partial=True,
        )
        ScheduleEvent.objects.filter(pk=self.slot.pk).update(tenancy=concurrent_tenancy)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.slot.refresh_from_db()
        # The edit itself went through.
        self.assertEqual(self.slot.name, 'שכירות ערב')

    def test_a_link_made_while_the_edit_was_open_stays(self):
        before = self.slot.updated_at
        self.edit_saved_after(self.tenancy)
        self.assertEqual(self.slot.tenancy_id, self.tenancy.id)
        self.assertGreater(self.slot.updated_at, before)

    def test_an_unlink_made_while_the_edit_was_open_stays(self):
        ScheduleEvent.objects.filter(pk=self.slot.pk).update(tenancy=self.tenancy)
        self.edit_saved_after(None)
        self.assertIsNone(self.slot.tenancy_id)
