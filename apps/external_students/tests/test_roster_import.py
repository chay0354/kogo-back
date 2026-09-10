"""
Reading a municipality's sheet.

Two guarantees carry this feature, and both are asserted here rather than
assumed. **No identity number is ever stored** — every real file carries one for
every child, and the owner's rule is that we keep none of them. And **a sheet
that was only half read cannot be applied** — a partial file looks exactly like
a class that emptied, and the difference is a register full of children.

The fixtures mirror the two real files that were analysed: a spreadsheet shaped
like Zamir's (a group header, a structured slots row, separator rows, people,
a stated count) and a scanned page shaped like Yehud's. The model is stubbed
throughout — these tests are about what we do with an answer, not about the
answer, and no test should spend money.
"""
from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from apps.core.models import Branch, City, UserProfile
from apps.courses.models import Course, CourseType, Lesson
from apps.customers.models import Payment, RecurringPayment
from apps.external_students import extraction, roster_import as service
from apps.external_students.extraction import (
    SheetColumns,
    SheetGroupBlock,
    SheetLayout,
    ScannedGroup,
    ScannedPerson,
)
from apps.external_students.models import (
    ExternalRosterGroup,
    ExternalRosterImport,
    ExternalRosterImportRow,
    ExternalRosterImportUnit,
    ExternalStudent,
)
from apps.instructors.models import Instructor

User = get_user_model()

IMPORTS_URL = '/api/v1/external-students/imports/'


def make_user(username, role, **extra):
    user = User.objects.create_user(username=username, password='pw-for-tests', **extra)
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return user


class RosterImportTestBase(APITestCase):
    """A branch shaped like a real external one, including the awkward parts."""

    def setUp(self):
        self.city = City.objects.create(name='עיר בדיקה')
        self.branch = Branch.objects.create(name='מרכז עירוני', city=self.city, is_external=True)
        self.own_branch = Branch.objects.create(name='סניף שלנו', city=self.city)
        self.ctype = CourseType.objects.create(name='קפוארה')
        self.manager = make_user('manager@imp.test', UserProfile.ROLE_MANAGER, email='manager@imp.test')
        self.worker = make_user('worker@imp.test', UserProfile.ROLE_WORKER, email='worker@imp.test')
        self.instructor = Instructor.objects.create(
            first_name='מורה', last_name='בדיקה', email='worker@imp.test',
            primary_branch=self.branch,
        )

        # Sunday 16:00 and Wednesday 16:00 of one course — the twice-weekly shape.
        self.twice = self._course('קפוארה ואקרובטיקה ג-ו', 5, 9)
        self.sunday = self._lesson(self.twice, day=0, start=time(16, 0))
        self.wednesday = self._lesson(self.twice, day=3, start=time(16, 0))

        # Monday 16:45, two different courses at the same time — the ambiguous case.
        self.young = self._course('3-4.5 קפוארה', 3, 4)
        self.older = self._course('4.5-6 קפוארה', 4, 6)
        self.young_monday = self._lesson(self.young, day=1, start=time(16, 45))
        self.older_monday = self._lesson(self.older, day=1, start=time(16, 45))

        self.auth(self.manager)

    def _course(self, name, min_age, max_age):
        return Course.objects.create(
            name=name, branch=self.branch, course_type=self.ctype,
            price=Decimal('235.00'), capacity=20, min_age=min_age, max_age=max_age,
            instructor=self.instructor,
        )

    def _lesson(self, course, day, start, end=None):
        return Lesson.objects.create(
            course=course, instructor=self.instructor, day_of_week=day,
            start_time=start, end_time=end or time(start.hour, start.minute + 45 - 60) if False else time(17, 30),
            is_recurring=True,
        )

    def auth(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')

    # -- fixtures shaped like the real files ------------------------------

    def zamir_grid(self):
        """
        A sheet in the shape the real one has, identity numbers and all.

        Columns: customer number | full name | ID | birth date | gender | mobile.
        A group header, a structured slots row, separators around the people, and
        the count the sheet states for itself.
        """
        return [
            ['311 - דוח משתתפים', 'לתאריכים:01/02/2026 - 28/02/2026', '', '', '', ''],
            ['מספר לקוח', 'שם מלא', 'ת.ז.', 'ת.לידה', 'מין', 'נייד'],
            ['37150030', 'אומנויות לחימה ג-ו ימים א+ד 16:00-16:45', '', '', '', ''],
            ['', '', 'א:16:00-16:45  ד:16:00-16:45', '', '', ''],
            ['', '==============================', '', '', '', ''],
            ['114403', 'גרינשטיין בן', '225801927', '2017-01-28', '', '054-4236987'],
            ['114772', 'בן יוחנה פלג', '343540639', '2017-09-24', '', '054-5759551'],
            ['', '==============================', '', '', '', ''],
            ['2', '', '', '', '', ''],
        ]

    def zamir_layout(self):
        return SheetLayout(
            period_label='01/02/2026 - 28/02/2026',
            stated_report_total=2,
            columns=SheetColumns(
                name_column=1, phone_column=5, second_phone_column=None,
                excluded_columns=[0, 2, 3, 4],
            ),
            groups=[SheetGroupBlock(
                municipality_code='37150030',
                group_name='אומנויות לחימה ג-ו ימים א+ד 16:00-16:45',
                slots_raw='א:16:00-16:45  ד:16:00-16:45',
                first_person_row=5, last_person_row=6, stated_total=2,
            )],
        )

    def yehud_page(self, people=None):
        return ScannedGroup(
            municipality_code='2378-0-1',
            group_name='גילאי 3-4.5 - שני',
            slots_raw='יום ב 16:45-17:30',
            stated_total=2,
            people=people if people is not None else [
                ScannedPerson(first_name='לב', last_name='ביידר', phone='0504442689'),
                ScannedPerson(first_name='דניא', last_name='בצקלה אדם', phone='054-7873560'),
            ],
        )

    def make_import(self, kind=ExternalRosterImport.KIND_XLSX, **extra):
        return ExternalRosterImport.objects.create(
            branch=self.branch, kind=kind, original_filename='sheet.xlsx',
            created_by=self.manager, **extra,
        )

    def seed_sheet_import(self):
        """An import segmented exactly as the upload path would leave it."""
        roster = self.make_import()
        layout = self.zamir_layout()
        with patch.object(extraction, 'read_sheet_layout', return_value=(layout, {'input_tokens': 10, 'output_tokens': 5})):
            with patch.object(extraction, 'read_grid', return_value=self.zamir_grid()):
                service.segment_spreadsheet(roster, b'x')
        roster.refresh_from_db()
        return roster

    def parse_all(self, roster):
        while True:
            unit = service.claim_next_unit(roster)
            if unit is None:
                break
            service.parse_unit(unit)
        roster.refresh_from_db()
        return roster


class NoIdentityNumberTests(RosterImportTestBase):
    """The owner's rule, asserted rather than trusted."""

    def test_the_identity_column_never_reaches_the_database(self):
        roster = self.parse_all(self.seed_sheet_import())
        unit = roster.units.first()
        unit.matched_lessons.set([self.sunday, self.wednesday])
        service._write_rows(unit, [
            {'first_name': 'בן', 'last_name': 'גרינשטיין', 'phone': '054-4236987'},
        ], [self.sunday])

        for model in (
            ExternalRosterImport, ExternalRosterImportUnit,
            ExternalRosterImportRow, ExternalStudent,
        ):
            for obj in model.objects.all():
                for field in obj._meta.get_fields():
                    if isinstance(field, (models.CharField, models.TextField)):
                        value = getattr(obj, field.name, '') or ''
                        self.assertNotRegex(
                            str(value), r'\b\d{9}\b',
                            f'{model.__name__}.{field.name} carries an identity number',
                        )

    def test_the_stored_grid_has_the_identifying_columns_emptied(self):
        roster = self.seed_sheet_import()
        flat = ' '.join(cell for row in roster.sheet_grid for cell in row)
        self.assertNotIn('225801927', flat)
        self.assertNotIn('343540639', flat)
        self.assertNotIn('2017-01-28', flat)
        # What we do keep is still there.
        self.assertIn('גרינשטיין בן', flat)
        self.assertIn('054-4236987', flat)

    def test_no_schema_has_a_field_for_one(self):
        self.assertNotIn('id_number', ScannedPerson.model_fields)
        self.assertNotIn('identity_number', ScannedPerson.model_fields)
        self.assertNotIn('tz', ScannedPerson.model_fields)

    def test_a_model_that_returns_one_fails_the_group_and_writes_nothing(self):
        """The scrub is a guard, not a hope."""
        roster = self.make_import(kind=ExternalRosterImport.KIND_PDF)
        ExternalRosterImportUnit.objects.create(
            roster_import=roster, ordinal=1, source_ref={'page': 'eA=='},
        )
        roster.units_total = 1
        roster.save(update_fields=['units_total'])

        leaked = self.yehud_page(people=[
            ScannedPerson(first_name='לב 225801927', last_name='ביידר', phone=''),
        ])
        with patch.object(extraction, 'read_scanned_page', return_value=(leaked, {'input_tokens': 1, 'output_tokens': 1})):
            self.parse_all(roster)

        unit = roster.units.first()
        self.assertEqual(unit.status, ExternalRosterImportUnit.STATUS_FAILED)
        self.assertEqual(ExternalRosterImportRow.objects.count(), 0)
        self.assertEqual(ExternalStudent.objects.count(), 0)


class SegmentAndParseTests(RosterImportTestBase):
    def test_a_sheet_becomes_one_unit_per_group(self):
        roster = self.seed_sheet_import()
        self.assertEqual(roster.units_total, 1)
        unit = roster.units.first()
        self.assertEqual(unit.municipality_code, '37150030')
        self.assertEqual(unit.slots, [{'day': 0, 'start': '16:00'}, {'day': 3, 'start': '16:00'}])
        self.assertEqual(unit.stated_total, 2)

    def test_people_come_from_cells_and_separators_do_not(self):
        roster = self.parse_all(self.seed_sheet_import())
        unit = roster.units.first()
        names = sorted(row.full_name for row in unit.rows.all())
        self.assertEqual(names, ['בן גרינשטיין', 'פלג בן יוחנה'])

    def test_a_count_that_disagrees_with_the_sheet_is_flagged_not_hidden(self):
        roster = self.make_import()
        layout = self.zamir_layout()
        layout.groups[0].stated_total = 9      # the sheet claims nine, we read two
        with patch.object(extraction, 'read_sheet_layout', return_value=(layout, {'input_tokens': 1, 'output_tokens': 1})):
            with patch.object(extraction, 'read_grid', return_value=self.zamir_grid()):
                service.segment_spreadsheet(roster, b'x')
        self.parse_all(roster)
        self.assertEqual(roster.units.first().status, ExternalRosterImportUnit.STATUS_MISMATCH)

    def test_a_scanned_page_is_read_into_the_same_shape(self):
        roster = self.make_import(kind=ExternalRosterImport.KIND_PDF)
        ExternalRosterImportUnit.objects.create(
            roster_import=roster, ordinal=1, source_ref={'page': 'eA=='},
        )
        roster.units_total = 1
        roster.save(update_fields=['units_total'])
        with patch.object(extraction, 'read_scanned_page', return_value=(self.yehud_page(), {'input_tokens': 1, 'output_tokens': 1})):
            self.parse_all(roster)

        unit = roster.units.first()
        self.assertEqual(unit.status, ExternalRosterImportUnit.STATUS_PARSED)
        self.assertEqual(unit.municipality_code, '2378-0-1')
        self.assertEqual(unit.slots, [{'day': 1, 'start': '16:45'}])
        self.assertEqual(unit.rows.count(), 2)

    def test_the_page_is_released_once_it_has_been_read(self):
        roster = self.make_import(kind=ExternalRosterImport.KIND_PDF)
        ExternalRosterImportUnit.objects.create(
            roster_import=roster, ordinal=1, source_ref={'page': 'eA=='},
        )
        roster.units_total = 1
        roster.save(update_fields=['units_total'])
        with patch.object(extraction, 'read_scanned_page', return_value=(self.yehud_page(), {'input_tokens': 1, 'output_tokens': 1})):
            self.parse_all(roster)
        self.assertNotIn('page', roster.units.first().source_ref)


class MatchingTests(RosterImportTestBase):
    def _unit(self, slots_raw, group_name='', code=''):
        roster = self.make_import()
        unit = ExternalRosterImportUnit.objects.create(
            roster_import=roster, ordinal=1, municipality_code=code,
            group_name=group_name, slots_raw=slots_raw,
            slots=extraction.parse_slots(slots_raw),
        )
        return unit

    def test_the_end_time_is_ignored(self):
        """Yehud writes 19:00-20:00 where our lesson is 19:00-19:45."""
        from apps.external_students.matching import match_unit

        late = self._lesson(self.twice, day=1, start=time(19, 0))
        unit = self._unit('יום ב 19:00-20:00')
        result = match_unit(unit)
        self.assertEqual(result['state'], ExternalRosterImportUnit.MATCH_EXACT)
        self.assertEqual([l.id for l in result['lessons']], [late.id])

    def test_two_courses_at_the_same_time_ask_rather_than_guess(self):
        from apps.external_students.matching import match_unit

        unit = self._unit('יום ב 16:45-17:30', group_name='קבוצה')
        result = match_unit(unit)
        self.assertEqual(result['state'], ExternalRosterImportUnit.MATCH_AMBIGUOUS)
        self.assertEqual(len(result['candidates']), 2)

    def test_the_age_band_breaks_the_tie(self):
        from apps.external_students.matching import match_unit

        unit = self._unit('יום ב 16:45-17:30', group_name='גילאי 3-4.5 - שני')
        result = match_unit(unit)
        self.assertEqual(result['state'], ExternalRosterImportUnit.MATCH_EXACT)
        self.assertEqual([l.id for l in result['lessons']], [self.young_monday.id])

    def test_a_twice_weekly_group_matches_both_lessons(self):
        from apps.external_students.matching import match_unit

        unit = self._unit('א:16:00-16:45  ד:16:00-16:45')
        result = match_unit(unit)
        self.assertEqual(result['state'], ExternalRosterImportUnit.MATCH_EXACT)
        self.assertEqual(
            {l.id for l in result['lessons']}, {self.sunday.id, self.wednesday.id},
        )

    def test_a_one_day_group_leaves_our_other_weekly_lesson_alone(self):
        """Yehud's Monday-only group against our twice-weekly course."""
        from apps.external_students.matching import match_unit

        unit = self._unit('א:16:00-16:45')
        result = match_unit(unit)
        self.assertEqual([l.id for l in result['lessons']], [self.sunday.id])

    def test_a_confirmed_mapping_is_used_next_month(self):
        from apps.external_students.matching import match_unit

        group = ExternalRosterGroup.objects.create(
            branch=self.branch, municipality_code='37150030', group_name='x',
        )
        group.lessons.set([self.sunday, self.wednesday])

        unit = self._unit('א:16:00-16:45  ד:16:00-16:45', code='37150030')
        result = match_unit(unit)
        self.assertEqual(result['state'], ExternalRosterImportUnit.MATCH_CONFIRMED)

    def test_a_mapping_whose_times_moved_is_not_trusted(self):
        from apps.external_students.matching import match_unit

        group = ExternalRosterGroup.objects.create(
            branch=self.branch, municipality_code='37150030', group_name='x',
        )
        group.lessons.set([self.sunday])          # one lesson, but the sheet says two
        unit = self._unit('א:16:00-16:45  ד:16:00-16:45', code='37150030')
        self.assertNotEqual(
            match_unit(unit)['state'], ExternalRosterImportUnit.MATCH_CONFIRMED,
        )


class ApplyTests(RosterImportTestBase):
    def _ready(self):
        roster = self.parse_all(self.seed_sheet_import())
        unit = roster.units.first()
        unit.matched_lessons.set([self.sunday, self.wednesday])
        unit.match_state = ExternalRosterImportUnit.MATCH_EXACT
        unit.save()
        return roster

    def test_a_twice_weekly_group_puts_each_child_on_both_lessons(self):
        roster = self._ready()
        service.apply_import(roster, user=self.manager)
        self.assertEqual(ExternalStudent.objects.filter(lesson=self.sunday).count(), 2)
        self.assertEqual(ExternalStudent.objects.filter(lesson=self.wednesday).count(), 2)

    def test_the_result_counts_children_not_rows(self):
        """
        A twice-weekly child holds two rows, and the review promised a number of
        children. Reporting rows here would say seven where it said five.
        """
        roster = self._ready()
        result = service.apply_import(roster, user=self.manager)
        self.assertEqual(ExternalStudent.objects.count(), 4)   # two children, two lessons
        self.assertEqual(result['added'], 2)

    def test_imported_children_say_they_came_from_a_file(self):
        roster = self._ready()
        service.apply_import(roster, user=self.manager)
        self.assertTrue(all(
            s.source == ExternalStudent.SOURCE_IMPORT for s in ExternalStudent.objects.all()
        ))

    def test_the_mapping_is_written_only_on_apply(self):
        roster = self._ready()
        self.assertEqual(ExternalRosterGroup.objects.count(), 0)
        service.apply_import(roster, user=self.manager)
        group = ExternalRosterGroup.objects.get(municipality_code='37150030')
        self.assertEqual(
            {l.id for l in group.lessons.all()}, {self.sunday.id, self.wednesday.id},
        )

    def test_a_half_read_sheet_cannot_be_applied(self):
        roster = self.seed_sheet_import()          # segmented, nothing parsed
        with self.assertRaises(service.ImportError_):
            service.apply_import(roster, user=self.manager)
        self.assertEqual(ExternalStudent.objects.count(), 0)

    def test_a_failed_group_blocks_until_it_is_skipped(self):
        roster = self._ready()
        unit = roster.units.first()
        unit.status = ExternalRosterImportUnit.STATUS_FAILED
        unit.save(update_fields=['status'])
        with self.assertRaises(service.ImportError_):
            service.apply_import(roster, user=self.manager)

        unit.status = ExternalRosterImportUnit.STATUS_SKIPPED
        unit.save(update_fields=['status'])
        service.apply_import(roster, user=self.manager)
        self.assertEqual(ExternalStudent.objects.count(), 0)

    def test_someone_the_sheet_no_longer_lists_is_removed_softly(self):
        leaving = ExternalStudent.objects.create(
            lesson=self.sunday, first_name='עזב', last_name='מזמן', phone='0509999999',
        )
        roster = self._ready()
        service.apply_import(roster, user=self.manager)
        leaving.refresh_from_db()
        self.assertFalse(leaving.is_active)
        self.assertIsNotNone(leaving.end_date)

    def test_a_lesson_the_sheet_never_mentioned_is_untouched(self):
        elsewhere = ExternalStudent.objects.create(
            lesson=self.young_monday, first_name='אחר', last_name='לגמרי',
        )
        roster = self._ready()
        service.apply_import(roster, user=self.manager)
        elsewhere.refresh_from_db()
        self.assertTrue(elsewhere.is_active)

    def test_stripping_most_of_a_lesson_needs_a_separate_yes(self):
        for i in range(4):
            ExternalStudent.objects.create(
                lesson=self.sunday, first_name=f'ותיק{i}', last_name='קבוצה',
            )
        roster = self._ready()
        self.assertIn(str(self.sunday.id), service.bulk_removal_lessons(roster))
        with self.assertRaises(service.ImportError_):
            service.apply_import(roster, user=self.manager)

        service.apply_import(
            roster, user=self.manager, confirmed_bulk_lessons=[str(self.sunday.id)],
        )
        self.assertEqual(
            ExternalStudent.objects.filter(lesson=self.sunday, is_active=True).count(), 2,
        )

    def test_applying_twice_does_not_duplicate_anyone(self):
        roster = self._ready()
        service.apply_import(roster, user=self.manager)
        before = ExternalStudent.objects.count()
        roster.status = ExternalRosterImport.STATUS_PARSED     # force a second pass
        roster.save(update_fields=['status'])
        self.assertEqual(ExternalStudent.objects.count(), before)


class ResumeTests(RosterImportTestBase):
    def test_two_callers_claim_two_different_groups(self):
        roster = self.make_import()
        roster.units_total = 2
        roster.save(update_fields=['units_total'])
        for ordinal in (1, 2):
            ExternalRosterImportUnit.objects.create(roster_import=roster, ordinal=ordinal)

        first = service.claim_next_unit(roster)
        second = service.claim_next_unit(roster)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.id, second.id)

    def test_a_group_stuck_mid_flight_is_offered_again(self):
        roster = self.make_import()
        roster.units_total = 1
        roster.save(update_fields=['units_total'])
        unit = ExternalRosterImportUnit.objects.create(
            roster_import=roster, ordinal=1,
            status=ExternalRosterImportUnit.STATUS_RUNNING,
            started_at=timezone.now() - service.RECLAIM_AFTER - timedelta(minutes=1),
        )
        self.assertEqual(service.claim_next_unit(roster).id, unit.id)

    def test_a_group_still_running_is_left_alone(self):
        roster = self.make_import()
        ExternalRosterImportUnit.objects.create(
            roster_import=roster, ordinal=1,
            status=ExternalRosterImportUnit.STATUS_RUNNING, started_at=timezone.now(),
        )
        self.assertIsNone(service.claim_next_unit(roster))

    def test_the_sweeper_finishes_what_a_closed_tab_left(self):
        roster = self.seed_sheet_import()
        self.assertEqual(roster.units_done, 0)
        service.sweep()
        roster.refresh_from_db()
        self.assertEqual(roster.units_done, 1)

    def test_an_import_nobody_came_back_to_is_closed_out(self):
        roster = self.seed_sheet_import()
        ExternalRosterImport.objects.filter(pk=roster.pk).update(
            created_at=timezone.now() - service.ABANDON_AFTER - timedelta(hours=1),
        )
        service.sweep()
        roster.refresh_from_db()
        self.assertEqual(roster.status, ExternalRosterImport.STATUS_FAILED)
        self.assertEqual(roster.sheet_grid, [])


class EndpointTests(RosterImportTestBase):
    def test_a_worker_cannot_reach_the_imports(self):
        self.auth(self.worker)
        self.assertEqual(self.client.get(IMPORTS_URL).status_code, status.HTTP_403_FORBIDDEN)

    def test_a_partner_cannot_either(self):
        """Reading a roster is one thing; deciding what a file means is the office's."""
        partner = make_user('partner@imp.test', UserProfile.ROLE_PARTNER, email='partner@imp.test')
        partner.profile.assigned_branches.add(self.branch)
        self.auth(partner)
        self.assertEqual(self.client.get(IMPORTS_URL).status_code, status.HTTP_403_FORBIDDEN)

    def test_a_non_external_branch_is_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        res = self.client.post(
            IMPORTS_URL,
            {'branch': str(self.own_branch.id), 'file': SimpleUploadedFile('a.xlsx', b'x')},
            format='multipart',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(ExternalRosterImport.objects.count(), 0)

    def test_an_unknown_file_type_is_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        res = self.client.post(
            IMPORTS_URL,
            {'branch': str(self.branch.id), 'file': SimpleUploadedFile('a.txt', b'x')},
            format='multipart',
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_stale_review_cannot_be_applied(self):
        roster = self.parse_all(self.seed_sheet_import())
        unit = roster.units.first()
        unit.matched_lessons.set([self.sunday])
        unit.save()

        stale = service.diff_digest(roster)
        ExternalStudent.objects.create(
            lesson=self.sunday, first_name='נוסף', last_name='ביד', phone='0501234567',
        )
        service._write_rows(unit, [], [self.sunday])       # the plan moved under us

        res = self.client.post(
            f'{IMPORTS_URL}{roster.id}/apply/',
            {'expected_digest': stale}, format='json',
        )
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)

    def test_the_cron_refuses_an_unauthenticated_caller(self):
        self.client.credentials()
        res = self.client.post('/api/v1/external-students/cron/roster-imports/')
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)


class NoLeakAfterAnImportTests(RosterImportTestBase):
    """The money guarantees again, this time after hundreds of rows arrive at once."""

    def test_a_full_import_creates_no_payment_and_moves_no_paying_count(self):
        from apps.enrollments.enrollment_counts import (
            count_capacity_enrollments,
            count_distinct_paying_children,
            count_paying_enrollments,
        )

        before = (
            count_paying_enrollments(lesson=self.sunday),
            count_distinct_paying_children(course=self.twice),
            count_capacity_enrollments(lesson=self.sunday),
        )

        roster = self.parse_all(self.seed_sheet_import())
        unit = roster.units.first()
        unit.matched_lessons.set([self.sunday, self.wednesday])
        unit.save()
        service.apply_import(roster, user=self.manager)

        self.assertTrue(ExternalStudent.objects.exists())
        self.assertEqual(before, (
            count_paying_enrollments(lesson=self.sunday),
            count_distinct_paying_children(course=self.twice),
            count_capacity_enrollments(lesson=self.sunday),
        ))
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(RecurringPayment.objects.count(), 0)

    def test_a_twice_weekly_child_counts_once_in_the_course(self):
        from apps.external_students.roster import external_counts_by_course

        roster = self.parse_all(self.seed_sheet_import())
        unit = roster.units.first()
        unit.matched_lessons.set([self.sunday, self.wednesday])
        unit.save()
        service.apply_import(roster, user=self.manager)

        self.assertEqual(ExternalStudent.objects.count(), 4)     # two children, two lessons
        self.assertEqual(external_counts_by_course([self.twice.id])[self.twice.id], 2)
