"""The trial lesson as a calendar event: what is written in it, and that both calendars can read it."""
import importlib
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from urllib.parse import parse_qs, urlsplit

from django.apps import apps as django_apps
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.core.models import Branch, City
from apps.core.tests.test_fixtures import TestDataFactory
from apps.courses.models import CourseType
from apps.customers import trial_calendar
from apps.customers.identification_models import WidgetRateBucket

EVENT = '/api/v1/customers/widget/trial-event/'
EVENT_FILE = '/api/v1/customers/widget/trial-event.ics'

# A Monday: the fifth of October 2026, when Israel is still on summer time (UTC+3).
MONDAY = date(2026, 10, 5)
TODAY = date(2026, 10, 2)


def _lesson(**course_type_fields):
    city, _ = City.objects.get_or_create(name='פתח תקווה')
    branch = Branch.objects.create(
        name='מרכז העיר - מינץ 24', address='מינץ 24', city=city,
        arrival_directions='נכנסים לחניה של סופר טל, והסטודיו משמאל.',
    )
    course_type = CourseType.objects.create(
        name='קפוארה', trial_bring_note='מכנס ארוך. בשיעור מורידים נעליים.', **course_type_fields,
    )
    course = TestDataFactory.create_course(name='קפוארה 3-4.5 בוי יום שני', branch=branch, course_type=course_type)
    instructor = TestDataFactory.create_instructor(first_name='ביגי', last_name='סתיו', branch=branch)
    # A lesson's days count from Sunday: 1 is a Monday.
    return TestDataFactory.create_lesson(
        course=course, instructor=instructor, day_of_week=1, start_time=time(16, 45), end_time=time(17, 30),
    )


def _unfolded(ics: str) -> list[str]:
    """The file's content lines, with the folded rows joined back."""
    lines = []
    for row in ics.split('\r\n'):
        if row.startswith(' ') and lines:
            lines[-1] += row[1:]
        elif row:
            lines.append(row)
    return lines


class TheEvent(TestCase):
    def setUp(self):
        self.lesson = _lesson()
        self.event = trial_calendar.build_trial_event(self.lesson, MONDAY, today=TODAY)

    def test_the_title_says_what_and_at_what_hour_with_the_short_name_of_the_field(self):
        self.assertEqual(self.event.title, 'שיעור ניסיון בקפוארה - 16:45')

    def test_a_field_with_a_short_name_is_called_by_it(self):
        CourseType.objects.filter(id=self.lesson.course.course_type_id).update(
            name='היפהופ - מחול', short_name='ריקוד',
        )
        self.lesson.course.course_type.refresh_from_db()

        event = trial_calendar.build_trial_event(self.lesson, MONDAY, today=TODAY)

        self.assertEqual(event.title, 'שיעור ניסיון בריקוד - 16:45')

    def test_the_notes_are_a_few_short_lines_the_most_needed_first(self):
        self.assertEqual(trial_calendar.plain_notes(self.event).split('\n'), [
            '🕒 יום שני 5.10, 16:45–17:30 (45 דקות)',
            '👤 בהדרכת ביגי סתיו',
            '🎒 להביא: מכנס ארוך. בשיעור מורידים נעליים.',
            '📍 מינץ 24, פתח תקווה',
            'נכנסים לחניה של סופר טל, והסטודיו משמאל.',
            '📞 שאלות? משרד קוגומלו 050-9424755',
        ])

    def test_the_address_and_the_city_stand_in_the_location(self):
        self.assertEqual(self.event.location, 'מינץ 24, פתח תקווה')

    def test_an_address_that_already_names_the_city_is_not_given_it_twice(self):
        Branch.objects.filter(id=self.lesson.course.branch_id).update(address='מינץ 24 פתח תקווה')
        self.lesson.course.branch.refresh_from_db()

        event = trial_calendar.build_trial_event(self.lesson, MONDAY, today=TODAY)

        self.assertEqual(event.location, 'מינץ 24 פתח תקווה')

    def test_what_is_not_known_is_left_out_and_never_written_empty(self):
        CourseType.objects.filter(id=self.lesson.course.course_type_id).update(trial_bring_note=None)
        Branch.objects.filter(id=self.lesson.course.branch_id).update(arrival_directions=None)
        self.lesson.instructor = None
        self.lesson.save()
        self.lesson.course.course_type.refresh_from_db()
        self.lesson.course.branch.refresh_from_db()

        event = trial_calendar.build_trial_event(self.lesson, MONDAY, today=TODAY)

        self.assertEqual(trial_calendar.plain_notes(event).split('\n'), [
            '🕒 יום שני 5.10, 16:45–17:30 (45 דקות)',
            '📍 מינץ 24, פתח תקווה',
            '📞 שאלות? משרד קוגומלו 050-9424755',
        ])

    def test_the_length_of_a_lesson_in_words(self):
        self.assertEqual(trial_calendar.duration_words(45), '45 דקות')
        self.assertEqual(trial_calendar.duration_words(60), 'שעה')
        self.assertEqual(trial_calendar.duration_words(90), 'שעה וחצי')

    def test_a_day_the_lesson_does_not_take_place_on_is_no_event(self):
        with self.assertRaises(trial_calendar.NoSuchEvent):
            trial_calendar.build_trial_event(self.lesson, date(2026, 10, 6), today=TODAY)

    def test_a_day_long_gone_or_far_ahead_is_no_event(self):
        with self.assertRaises(trial_calendar.NoSuchEvent):
            trial_calendar.build_trial_event(self.lesson, date(2026, 9, 21), today=TODAY)
        with self.assertRaises(trial_calendar.NoSuchEvent):
            trial_calendar.build_trial_event(self.lesson, date(2027, 6, 7), today=TODAY)


class ForGoogle(TestCase):
    def setUp(self):
        self.event = trial_calendar.build_trial_event(_lesson(), MONDAY, today=TODAY)
        self.url = urlsplit(trial_calendar.google_url(self.event))
        self.query = {key: value[0] for key, value in parse_qs(self.url.query).items()}

    def test_the_link_opens_the_event_template(self):
        self.assertEqual(f'{self.url.scheme}://{self.url.netloc}{self.url.path}', 'https://calendar.google.com/calendar/render')
        self.assertEqual(self.query['action'], 'TEMPLATE')
        self.assertEqual(self.query['text'], 'שיעור ניסיון בקפוארה - 16:45')
        self.assertEqual(self.query['location'], 'מינץ 24, פתח תקווה')

    def test_the_hours_are_israels_whatever_the_phone_thinks(self):
        """16:45 in Israel on a summer-time day is 13:45 UTC; both ends carry the Z."""
        self.assertEqual(self.query['dates'], '20261005T134500Z/20261005T143000Z')
        self.assertEqual(self.query['ctz'], 'Asia/Jerusalem')

    def test_winter_time_is_two_hours_from_utc(self):
        event = trial_calendar.build_trial_event(_lesson(), date(2026, 11, 2), today=date(2026, 10, 30))

        query = parse_qs(urlsplit(trial_calendar.google_url(event)).query)

        self.assertEqual(query['dates'][0], '20261102T144500Z/20261102T153000Z')

    def test_the_notes_are_set_in_bold_where_it_helps_with_a_line_break_between_lines(self):
        details = self.query['details']

        self.assertEqual(details.split('<br>'), [
            '🕒 <b>יום שני 5.10, 16:45–17:30</b> (45 דקות)',
            '👤 בהדרכת <b>ביגי סתיו</b>',
            '🎒 <b>להביא:</b> מכנס ארוך. בשיעור מורידים נעליים.',
            '📍 <b>מינץ 24, פתח תקווה</b>',
            'נכנסים לחניה של סופר טל, והסטודיו משמאל.',
            '📞 <b>שאלות?</b> משרד קוגומלו <a href="tel:0509424755">050-9424755</a>',
        ])

    def test_markup_in_a_name_is_written_as_text(self):
        lesson = _lesson()
        lesson.instructor.first_name = '<b>דני</b>'
        lesson.instructor.save()

        details = trial_calendar.google_notes(trial_calendar.build_trial_event(lesson, MONDAY, today=TODAY))

        self.assertIn('&lt;b&gt;דני&lt;/b&gt;', details)


class ForAppleAndTheRest(TestCase):
    def setUp(self):
        self.event = trial_calendar.build_trial_event(_lesson(), MONDAY, today=TODAY)
        self.ics = trial_calendar.ics_file(self.event, now=datetime(2026, 10, 2, 9, 0, tzinfo=dt_timezone.utc))
        self.lines = _unfolded(self.ics)

    def test_it_is_one_event_in_one_calendar(self):
        self.assertEqual(self.lines[0], 'BEGIN:VCALENDAR')
        self.assertEqual(self.lines[-1], 'END:VCALENDAR')
        self.assertEqual(self.lines.count('BEGIN:VEVENT'), 1)
        self.assertIn('VERSION:2.0', self.lines)
        self.assertIn('METHOD:PUBLISH', self.lines)

    def test_every_line_ends_as_the_standard_asks_and_none_is_longer_than_75_octets(self):
        self.assertTrue(self.ics.endswith('\r\n'))
        self.assertNotIn('\n', self.ics.replace('\r\n', ''))
        for row in self.ics.split('\r\n'):
            self.assertLessEqual(len(row.encode('utf-8')), 75, row)

    def test_no_row_ends_in_the_middle_of_an_escape(self):
        for row in self.ics.split('\r\n'):
            trailing = len(row) - len(row.rstrip('\\'))
            self.assertEqual(trailing % 2, 0, row)

    def test_a_folded_line_reads_back_whole(self):
        """A Hebrew letter is two octets: a line cut inside one would not decode at all."""
        summary = next(line for line in self.lines if line.startswith('SUMMARY:'))
        self.assertEqual(summary, 'SUMMARY:שיעור ניסיון בקפוארה - 16:45')

    def test_the_hours_are_in_utc(self):
        self.assertIn('DTSTART:20261005T134500Z', self.lines)
        self.assertIn('DTEND:20261005T143000Z', self.lines)
        self.assertIn('DTSTAMP:20261002T090000Z', self.lines)

    def test_commas_and_new_lines_are_escaped(self):
        self.assertIn('LOCATION:מינץ 24\\, פתח תקווה', self.lines)
        description = next(line for line in self.lines if line.startswith('DESCRIPTION:🕒'))
        self.assertIn('16:45–17:30 (45 דקות)\\n👤 בהדרכת ביגי סתיו\\n', description)
        self.assertIn('נכנסים לחניה של סופר טל\\, והסטודיו משמאל.', description)

    def test_the_same_lesson_on_the_same_day_is_the_same_event(self):
        self.assertIn(f'UID:trial-{self.event.uid.split("-", 1)[1]}', self.lines)
        again = trial_calendar.build_trial_event(_lesson(), MONDAY, today=TODAY)
        self.assertNotEqual(again.uid, self.event.uid)  # another lesson, another event

    def test_it_reminds_an_hour_before(self):
        self.assertIn('BEGIN:VALARM', self.lines)
        self.assertIn('TRIGGER:-PT60M', self.lines)


class TheEndpoints(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.lesson = _lesson()
        today = datetime.now(trial_calendar.LOCAL_TIME).date()
        # The next Monday from today, so the test holds on any day it is run.
        self.monday = today + timedelta(days=(0 - today.weekday()) % 7 or 7)
        self.query = {'lesson_id': str(self.lesson.id), 'date': self.monday.isoformat()}

    def test_the_form_is_given_the_title_the_google_link_and_the_file(self):
        response = self.client.get(EVENT, self.query)

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['title'], 'שיעור ניסיון בקפוארה - 16:45')
        self.assertTrue(body['google_url'].startswith('https://calendar.google.com/calendar/render?action=TEMPLATE'))
        self.assertEqual(
            body['ics_path'],
            f'customers/widget/trial-event.ics?lesson_id={self.lesson.id}&date={self.monday.isoformat()}',
        )

    def test_the_file_is_served_as_a_calendar(self):
        response = self.client.get(EVENT_FILE, self.query)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/calendar; charset=utf-8')
        self.assertIn('inline', response['Content-Disposition'])
        self.assertIn('SUMMARY:', response.content.decode('utf-8'))

    def test_a_wrong_day_an_unknown_lesson_and_a_broken_request_are_not_found(self):
        tuesday = (self.monday + timedelta(days=1)).isoformat()
        for query in (
            {**self.query, 'date': tuesday},
            {**self.query, 'lesson_id': '11111111-1111-1111-1111-111111111111'},
            {**self.query, 'lesson_id': 'not-an-id'},
            {**self.query, 'date': 'tomorrow'},
            {},
        ):
            self.assertEqual(self.client.get(EVENT, query).status_code, 404, query)
            self.assertEqual(self.client.get(EVENT_FILE, query).status_code, 404, query)

    def test_a_course_that_is_closed_has_no_event(self):
        self.lesson.course.is_active = False
        self.lesson.course.save()

        self.assertEqual(self.client.get(EVENT, self.query).status_code, 404)

    def test_asking_is_counted_by_address(self):
        self.client.get(EVENT, self.query)

        self.assertTrue(WidgetRateBucket.objects.filter(scope='calendar').exists())


class WhatTheOwnerDictated(TestCase):
    """The data the two migrations fill in: which branch gets which directions, which field which note."""

    def test_each_studio_gets_its_own_way_in_and_the_others_none(self):
        petah_tikva = City.objects.create(name='פתח תקווה')
        mintz = Branch.objects.create(name='מרכז העיר - מינץ 24', address='מינץ 24', city=petah_tikva)
        dimri = Branch.objects.create(name='קניון דמרי סנטר', address='אנג׳ל 78', city=City.objects.create(name='כפר סבא'))
        rosh = Branch.objects.create(name='קרל וגרטי קורי 8', address='קרל וגרטי קורי 8', city=City.objects.create(name='ראש העין'))
        shoham = Branch.objects.create(name='ביה״ס ניצנים - צורן 6', address='צורן 6', city=City.objects.create(name='שוהם'))
        other = Branch.objects.create(name='אם המושבות, רפאל איתן 5', address='רפאל איתן 5', city=petah_tikva)
        kept = Branch.objects.create(name='מינץ הישן', address='מינץ 24 ב', arrival_directions='כבר נכתב', city=petah_tikva)

        importlib.import_module('apps.core.migrations.0029_trial_calendar_details').fill_directions(django_apps, None)

        for branch in (mintz, dimri, rosh, shoham, other, kept):
            branch.refresh_from_db()
        self.assertIn('סופר טל', mintz.arrival_directions)
        self.assertIn('קומה מינוס 1', dimri.arrival_directions)
        self.assertIn('מתחם הקבלנים', rosh.arrival_directions)
        self.assertIn('אולם הספורט', shoham.arrival_directions)
        self.assertIsNone(other.arrival_directions)
        self.assertEqual(kept.arrival_directions, 'כבר נכתב')

    def test_each_field_gets_what_to_come_with(self):
        names = ['קפוארה', 'היפהופ - מחול', 'אקרובטיקה אווירית', 'אקרודאנס', 'ברייקדאנס']
        for name in names:
            CourseType.objects.create(name=name)

        importlib.import_module('apps.courses.migrations.0022_trial_calendar_details').fill_notes(django_apps, None)

        notes = {t.name: (t.trial_bring_note, t.short_name) for t in CourseType.objects.filter(name__in=names)}
        self.assertEqual(notes['קפוארה'], ('מכנס ארוך. בשיעור מורידים נעליים.', None))
        self.assertEqual(notes['היפהופ - מחול'], ('שיער אסוף, טייץ ונעליים.', 'ריקוד'))
        self.assertEqual(notes['אקרובטיקה אווירית'], ('טייץ ארוך. מורידים נעליים בתחילת השיעור.', None))
        self.assertEqual(notes['אקרודאנס'], (None, None))
        self.assertEqual(notes['ברייקדאנס'], (None, None))
