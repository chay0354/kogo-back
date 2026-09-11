"""The tenancy's own rules: VAT on the monthly amount, the billing day, and the contract's monthly sum."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from apps.rentals.models import Tenancy
from apps.rentals.slots import id_digits, suggested_monthly_amount
from apps.rentals.tests.factories import make_branch, make_customer, make_rental


class TenancyModelTests(TestCase):
    def setUp(self):
        self.branch = make_branch('פלורנטין')
        self.tenant = make_customer()

    def test_starts_as_a_draft_billed_on_the_first(self):
        tenancy = Tenancy.objects.create(tenant=self.tenant, branch=self.branch, monthly_amount=Decimal('1000'))
        self.assertEqual(tenancy.status, Tenancy.STATUS_DRAFT)
        self.assertEqual(tenancy.get_status_display(), 'טיוטה')
        self.assertEqual(tenancy.billing_day, 1)

    def test_status_labels_are_the_offices_words(self):
        self.assertEqual(
            [label for _, label in Tenancy.STATUS_CHOICES],
            ['טיוטה', 'נשלח', 'נחתם', 'פעיל', 'הסתיים', 'בוטל'],
        )

    def test_monthly_total_adds_vat(self):
        tenancy = Tenancy(tenant=self.tenant, monthly_amount=Decimal('1000.00'))
        self.assertEqual(tenancy.monthly_total, Decimal('1180.00'))
        # 333.33 × 1.18 = 393.3294
        tenancy.monthly_amount = Decimal('333.33')
        self.assertEqual(tenancy.monthly_total, Decimal('393.33'))

    def test_billing_day_must_exist_in_every_month(self):
        for day in (0, 29, 31):
            with self.subTest(day=day), self.assertRaises(ValidationError):
                Tenancy(tenant=self.tenant, monthly_amount=Decimal('1'), billing_day=day).full_clean()
        Tenancy(tenant=self.tenant, monthly_amount=Decimal('1'), billing_day=28).full_clean()

    def test_the_database_refuses_a_day_some_months_lack(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Tenancy.objects.create(tenant=self.tenant, monthly_amount=Decimal('1'), billing_day=30)

    def test_the_database_refuses_a_negative_amount(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Tenancy.objects.create(tenant=self.tenant, monthly_amount=Decimal('-1'))

    def test_a_tenant_with_an_agreement_is_protected(self):
        Tenancy.objects.create(tenant=self.tenant, monthly_amount=Decimal('1'))
        with self.assertRaises(ProtectedError):
            self.tenant.delete()

    def test_deleting_a_tenancy_frees_its_slots(self):
        tenancy = Tenancy.objects.create(tenant=self.tenant, branch=self.branch, monthly_amount=Decimal('1'))
        slot = make_rental(self.branch, tenancy=tenancy)
        tenancy.delete()
        slot.refresh_from_db()
        self.assertIsNone(slot.tenancy_id)


class SuggestedMonthlyAmountTests(TestCase):
    """Σ price_per_session × 4 × weekdays — the rental contract PDF's "rate × 4" per weekday."""

    def setUp(self):
        self.branch = make_branch('פלורנטין')

    def test_four_weeks_of_every_weekday_rented(self):
        slots = [
            make_rental(self.branch, price='150', days=(0, 3)),  # 150 × 4 × 2 = 1200
            make_rental(self.branch, price='90', days=(2,)),     # 90 × 4 × 1 = 360
        ]
        self.assertEqual(suggested_monthly_amount(slots), Decimal('1560.00'))

    def test_a_slot_with_no_weekdays_counts_one(self):
        slot = make_rental(self.branch, price='120', days=())
        self.assertEqual(suggested_monthly_amount([slot]), Decimal('480.00'))

    def test_a_one_time_rental_adds_nothing_to_a_month(self):
        # Not monthly: the PDF bills it once. The tenants screen agrees.
        once = make_rental(self.branch, price='200', days=(), event_type='one_time')
        weekly = make_rental(self.branch, price='100', days=(1,))
        self.assertEqual(suggested_monthly_amount([once]), Decimal('0.00'))
        self.assertEqual(suggested_monthly_amount([once, weekly]), Decimal('400.00'))

    def test_a_weekday_listed_twice_is_one_weekday(self):
        slot = make_rental(self.branch, price='100', days=(1, 1, '1'))
        self.assertEqual(suggested_monthly_amount([slot]), Decimal('400.00'))

    def test_inactive_slots_are_left_out(self):
        slots = [
            make_rental(self.branch, price='100', days=(1,)),
            make_rental(self.branch, price='500', days=(2,), is_active=False),
        ]
        self.assertEqual(suggested_monthly_amount(slots), Decimal('400.00'))

    def test_agorot_are_kept(self):
        slot = make_rental(self.branch, price='99.99', days=(0, 1, 2))
        self.assertEqual(suggested_monthly_amount([slot]), Decimal('1199.88'))

    def test_no_slots_is_zero(self):
        self.assertEqual(suggested_monthly_amount([]), Decimal('0.00'))

    def test_id_digits_reads_an_id_however_it_was_typed(self):
        self.assertEqual(id_digits('51-234567-8'), '512345678')
        self.assertEqual(id_digits(' 0123 4567 8 '), '012345678')
        self.assertEqual(id_digits(None), '')
