"""
The dates the bot says a trial lesson can be booked on are the dates the
registration form offers — the same ones, on the same data
(docs/WAHUB-BOT-ACCURACY-2026-10-12.md).

`TheForm` below is a parent on the site: it reads the catalogue, decides which
card shows a trial button the way the page does, opens the date picker, and
books every date the picker lets it choose — through the form's own public
endpoints, the submit included (each booking is rolled back). What it could
book is held against what the bot's tool returned, state by state: a full
date, a full class, a cancelled lesson, a closed day, trials closed, today,
tomorrow, a paid trial, a class of two days, a track.

The page's own rules are the front's (kogo-front src/app/widget:
catalogRows.ts, alternativeLessons.ts, lessonVisibility.ts, page.tsx); they
are written here as the page has them, each beside its name.
"""
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import patch

from django.db import connection, transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.tests.test_fixtures import TestDataFactory
from apps.courses.models import LessonBundle
from apps.enrollments.models import LessonEnrollment, TrialBlockedDate, TrialRegistrationPolicy
from apps.enrollments.trial_reminders import iter_upcoming_lesson_occurrences
from apps.scheduling.models import LessonCancellation
from apps.wahub import course_tools, trial_offer
from apps.wahub.models import Contact
from apps.wahub.shadow_tools import ToolContext, run_tool
from apps.wahub.tests.base import PHONE, WahubTestCase

CATALOG = '/api/v1/customers/widget/courses/'
DATES = '/api/v1/customers/widget/lesson-occurrences/'
TRIAL = '/api/v1/customers/widget/trial-register/'
NOTIFY = 'apps.enrollments.trial_reminders.stamp_and_notify_trial_enrollment'
INSTRUCTORS_AGE_GROUP = 16

# A Monday morning. Sunday is day 0 of a lesson, so a lesson of day 1 is today and one of day 2 is tomorrow.
MONDAY = date(2027, 2, 1)
_real_localtime = timezone.localtime


def moment(day: date = MONDAY, hour: int = 10, minute: int = 0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


@contextmanager
def clock_at(now):
    """The form's clock: `timezone.localtime()` answers `now`; a moment handed to it is still converted."""
    def localtime(value=None, tz=None):
        return now if value is None else _real_localtime(value, tz)

    with patch('django.utils.timezone.localtime', side_effect=localtime):
        yield


class TheForm:
    """A parent on the site, as far as a trial lesson goes."""

    def __init__(self, branch):
        self.branch = branch
        self.client = APIClient()

    def catalogue(self) -> dict:
        response = self.client.get(CATALOG, {'branch_id': str(self.branch.id)})
        assert response.status_code == 200, response.content
        return {row['id']: row for row in response.json()}

    @staticmethod
    def rows(course: dict) -> list:
        """catalogRows.buildCatalogRows: a row is a lesson or a track. The instructors' track shows no single day."""
        instructors = course['min_age'] == INSTRUCTORS_AGE_GROUP and course['max_age'] == INSTRUCTORS_AGE_GROUP
        rows = [] if instructors else [(lesson, None) for lesson in course['lessons']]
        for bundle in course['bundles']:
            if instructors and len(bundle['lessons']) < 2:
                continue
            rows.append((None, bundle))
        return rows

    def picker(self, course: dict, lesson, bundle):
        """The rows of the date picker behind a card's trial button — None when the card has no such button."""
        # page.tsx handleEnrollClick: a class with a link of its own opens the link, not the form.
        if (course.get('external_link') or '').strip():
            return None
        # alternativeLessons.isWidgetSelectionFull: a full selection shows "מלא" and no button.
        if (bundle or lesson)['is_full']:
            return None
        # lessonVisibility.trialRegistrationOffered / trialLessonChoices.
        choices = [lesson] if lesson else bundle['lessons']
        choices = [row for row in choices if row.get('trial_registration_open') is not False and row.get('trial_is_full') is not True]
        if not choices:
            return None
        # CourseRegistrationForm: one lesson asks by lesson_id, several by lesson_ids — three dates.
        ids = [row['id'] for row in choices]
        params = {'lesson_id': ids[0], 'count': 3} if len(ids) == 1 else {'lesson_ids': ','.join(ids), 'count': 3}
        response = self.client.get(DATES, params)
        assert response.status_code == 200, response.content
        return response.json()

    def book(self, course_id: str, lesson_id: str, day: str) -> bool:
        """The submit, for real — and undone. True when the form took the booking."""
        with transaction.atomic(), patch(NOTIFY, return_value={'sent': False}):
            response = self.client.post(TRIAL, {
                'parent_id_number': '123456782', 'parent_first_name': 'רות', 'parent_last_name': 'בדויה',
                'parent_phone': '0521234567',
                'child_first_name': 'נועה', 'child_last_name': 'בדויה', 'child_id_number': '111111118',
                'child_birth_date': '2018-05-05', 'child_gender': 'female',
                'course_id': course_id, 'lesson_id': lesson_id, 'trial_lesson_date': day,
            }, format='json')
            transaction.set_rollback(True)
        assert response.status_code in (201, 400, 404), response.content
        return response.status_code == 201

    def offered(self, course_id) -> dict:
        """
        {'open': dates a parent can choose and book, 'full': dates shown and not
        bookable} — each a set of (lesson id, ISO date). A class that is not in
        the catalogue offers nothing.
        """
        course = self.catalogue().get(str(course_id))
        found = {'open': set(), 'full': set()}
        if course is None:
            return found
        for lesson, bundle in self.rows(course):
            for row in self.picker(course, lesson, bundle) or []:
                key = (row['lesson_id'], row['date'])
                booked = self.book(course['id'], row['lesson_id'], row['date'])
                if row['is_full']:
                    assert not booked, f'the form booked a date it showed as full: {key}'
                found['open' if booked else 'full'].add(key)
        found['full'] -= found['open']
        return found


@override_settings(SUBSCRIPTION_FIRST_CHARGE_DATE='', BLOCKED_TRIAL_LESSON_DATES='', REGISTRATION_FEE_ILS=120, ANTHROPIC_API_KEY='')
class TrialOfferTestCase(WahubTestCase):
    """One branch, built per test, on a Monday morning."""

    now = moment()

    def setUp(self):
        super().setUp()
        TrialRegistrationPolicy.current()
        self.branch = TestDataFactory.create_branch(name='סטודיו הבדיקה', city=TestDataFactory.create_city('ראש העין'))
        self.field = TestDataFactory.create_course_type('קפוארה')
        self.room = TestDataFactory.create_room(name='אולם', branch=self.branch, capacity=10)
        self.instructor = TestDataFactory.create_instructor(first_name='אורי', last_name='דוגמאי', branch=self.branch)
        self.form = TheForm(self.branch)
        self._people = 0

    # --- builders ---

    def course(self, name='קפוארה כיתות א-ב', *, days=((2, time(17, 0), time(17, 45)),), **fields):
        fields.setdefault('capacity', 12)
        fields.setdefault('course_type', self.field)
        row = TestDataFactory.create_course(name=name, branch=self.branch, price=Decimal('260.00'), **fields)
        lessons = [
            TestDataFactory.create_lesson(
                course=row, room=self.room, instructor=self.instructor, day_of_week=day, start_time=start, end_time=end,
            )
            for day, start, end in days
        ]
        return row, lessons

    def track(self, course, lessons, **fields):
        bundle = LessonBundle.objects.create(course=course, name='פעמיים בשבוע', combined_price=Decimal('400.00'), **fields)
        bundle.lessons.set(lessons)
        return bundle

    def child(self, *, status='active', first_name=None, phone=None):
        self._people += 1
        family = TestDataFactory.create_family(name=f'משפחה {self._people}', branch=self.branch, phone=phone or f'05055{self._people:05d}')
        return TestDataFactory.create_child(family=family, first_name=first_name or f'ילד{self._people}', status=status)

    def pays(self, lesson, how_many=1):
        for _ in range(how_many):
            LessonEnrollment.objects.create(child=self.child(), lesson=lesson, status='active')

    def trials(self, lesson, day, how_many=1):
        for _ in range(how_many):
            LessonEnrollment.objects.create(
                child=self.child(status='trial_signed'), lesson=lesson, status='active', trial_lesson_date=day,
            )

    def coming(self, lesson, count=3):
        return iter_upcoming_lesson_occurrences(lesson, count=count, now=self.now)

    # --- the two sides ---

    def ctx(self, contact=None):
        return ToolContext(contact, now=self.now)

    def card(self, course, ctx=None) -> dict:
        result = run_tool('course_card', {'course_id': str(course.id)}, ctx or self.ctx())
        self.assertTrue(result.get('found'), result)
        return result['course']

    def offer(self, course):
        loaded = course_tools.with_lessons(type(course).objects.filter(id=course.id)).get()
        return trial_offer.for_course(loaded, trial_offer.Seats([loaded], now=self.now))

    def same(self, course, *, but_started=()):
        """The form and the bot's tool, on the same data at the same moment: the same dates. Returns (form, trial payload)."""
        with clock_at(self.now):
            form = self.form.offered(course.id)
            offer = self.offer(course)
            trial = self.card(course)['trial']
        open_ = {(str(slot.lesson.id), slot.day.isoformat()) for slot in offer.open}
        full = {(str(slot.lesson.id), slot.day.isoformat()) for slot in offer.full}
        started = {(str(lesson.id), day.isoformat()) for lesson, day in but_started}
        self.assertEqual(open_, form['open'] - started, 'the dates that can be booked')
        if offer.slots:
            self.assertEqual(full, form['full'], 'the dates shown as full')
        # What the tool hands the bot is that offer, and nothing beside it.
        self.assertEqual(
            [(row['lesson_id'], row['date']) for row in trial['dates']],
            [(str(slot.lesson.id), slot.day.strftime('%d.%m.%Y')) for slot in offer.open],
        )
        self.assertEqual(trial['can_book'], bool(form['open'] - started))
        self.assertEqual(sorted(row['date'] for row in trial.get('full_dates', [])), sorted(slot.day.strftime('%d.%m.%Y') for slot in offer.full))
        return form, trial


class SameDatesTests(TrialOfferTestCase):
    def test_a_weekly_lesson_offers_its_three_coming_dates(self):
        course, (lesson,) = self.course()
        form, trial = self.same(course)
        self.assertEqual(sorted(day for _lesson, day in form['open']), ['2027-02-02', '2027-02-09', '2027-02-16'])
        self.assertEqual([row['date'] for row in trial['dates']], ['02.02.2027', '09.02.2027', '16.02.2027'])
        self.assertEqual((trial['is_paid'], trial['price']), (False, None))
        self.assertEqual(trial['dates_text'], 'אפשר להגיע לשיעור ניסיון ביום שלישי בשעה 17:00, בתאריכים 02.02 (מחר), 09.02 או 16.02')

    def test_a_paid_trial_has_the_same_dates_and_its_price(self):
        course, _lessons = self.course(trial_lesson_is_paid=True, trial_lesson_price=Decimal('30.00'))
        form, trial = self.same(course)
        self.assertEqual(len(form['open']), 3)
        self.assertEqual((trial['is_paid'], trial['price'], trial['text']), (True, '30.00', 'שיעור ניסיון בעלות ₪30'))

    def test_a_paid_trial_with_no_price_cannot_be_booked_on_any_date(self):
        course, _lessons = self.course(trial_lesson_is_paid=True, trial_lesson_price=None)
        form, trial = self.same(course)
        self.assertEqual(form['open'], set())                       # the picker shows dates; the submit refuses each
        self.assertEqual(len(form['full']), 3)
        self.assertEqual((trial['can_book'], trial['dates']), (False, []))
        self.assertIn('לבדוק במשרד', trial['dates_text'])

    def test_the_nearest_date_is_full_and_the_next_is_not(self):
        course, (lesson,) = self.course()
        first, second, _third = self.coming(lesson)
        self.pays(lesson, 4)
        self.trials(lesson, first, 6)                                # the room holds ten: four pay, six try that day
        form, trial = self.same(course)
        self.assertEqual(form['full'], {(str(lesson.id), first.isoformat())})
        self.assertEqual([row['date'] for row in trial['dates']], ['09.02.2027', '16.02.2027'])
        self.assertEqual(trial['full_dates'], [{'date': '02.02.2027', 'day': 'שלישי', 'start': '17:00'}])
        self.assertEqual(trial['dates'][0]['trial_seats_left'], 6)
        self.assertNotIn('02.02', trial['dates_text'])
        self.assertEqual(self.card(course)['lessons'][0]['trial_dates'], ['09.02.2027', '16.02.2027'])

    def test_every_coming_date_is_full(self):
        course, (lesson,) = self.course()
        self.pays(lesson, 8)
        for day in self.coming(lesson):
            self.trials(lesson, day, 2)
        form, trial = self.same(course)
        self.assertEqual(form, {'open': set(), 'full': set()})       # the card shows "התפוסה מלאה לשיעור ניסיון" and no button
        self.assertEqual((trial['can_book'], trial['dates'], trial['full']), (False, [], True))
        self.assertEqual(trial['dates_text'], 'כל התאריכים הקרובים לשיעור ניסיון מלאים')
        self.assertEqual(self.card(course)['lessons'][0]['trial_status'], 'מלא לניסיון בתאריכים הקרובים')

    def test_a_class_with_no_seat_left_offers_no_trial(self):
        course, (lesson,) = self.course()
        self.pays(lesson, 10)
        form, trial = self.same(course)
        self.assertEqual(form['open'], set())
        self.assertEqual((trial['can_book'], trial['dates']), (False, []))
        self.assertIn('החוג מלא כרגע', trial['dates_text'])
        lesson_row = self.card(course)['lessons'][0]
        self.assertEqual((lesson_row['is_full'], lesson_row['seats_left'], lesson_row['trial_status']), (True, 0, 'המועד מלא'))

    def test_the_tightest_limit_decides_lesson_group_or_room(self):
        course, (lesson,) = self.course()
        lesson.capacity = 3                                          # the lesson's own limit: tighter than the group (12) and the room (10)
        lesson.save(update_fields=['capacity'])
        first = self.coming(lesson)[0]
        self.pays(lesson, 1)
        self.trials(lesson, first, 2)
        form, trial = self.same(course)
        self.assertEqual(form['full'], {(str(lesson.id), first.isoformat())})
        self.assertEqual([row['trial_seats_left'] for row in trial['dates']], [2, 2])

    def test_a_cancelled_lesson_is_not_a_date(self):
        course, (lesson,) = self.course()
        first = self.coming(lesson)[0]
        LessonCancellation.objects.create(lesson=lesson, occurrence_date=first)
        form, trial = self.same(course)
        self.assertEqual(sorted(day for _lesson, day in form['open']), ['2027-02-09', '2027-02-16', '2027-02-23'])
        self.assertEqual(trial['not_offered'], [{'date': '02.02.2027', 'day': 'שלישי', 'start': '17:00', 'why': 'השיעור בתאריך הזה בוטל'}])
        self.assertNotIn('02.02', trial['dates_text'])

    def test_a_day_the_office_closed_to_trials_is_not_a_date(self):
        course, (lesson,) = self.course()
        other, (other_lesson,) = self.course('היפ הופ כיתות א-ב', days=((2, time(18, 0), time(18, 45)),))
        first, second, _third = self.coming(lesson)
        TrialBlockedDate.objects.create(date=first, reason='חופשת סוכות')
        only_here = TrialBlockedDate.objects.create(date=second, reason='אירוע באולם')
        only_here.lessons.set([lesson])
        form, trial = self.same(course)
        self.assertEqual(sorted(day for _lesson, day in form['open']), ['2027-02-16', '2027-02-23', '2027-03-02'])
        self.assertEqual([(row['date'], row['why']) for row in trial['not_offered']], [
            ('02.02.2027', 'אין שיעורי ניסיון בתאריך הזה (חופשת סוכות)'), ('09.02.2027', 'אין שיעורי ניסיון בתאריך הזה (אירוע באולם)'),
        ])
        # The day closed for one lesson is open for the lesson beside it.
        other_form, _other_trial = self.same(other)
        self.assertEqual(sorted(day for _lesson, day in other_form['open']), ['2027-02-09', '2027-02-16', '2027-02-23'])

    @override_settings(BLOCKED_TRIAL_LESSON_DATES='2027-02-02,2027-02-09')
    def test_the_holidays_fixed_in_the_settings_are_not_dates(self):
        course, _lessons = self.course()
        form, trial = self.same(course)
        self.assertEqual(sorted(day for _lesson, day in form['open']), ['2027-02-16', '2027-02-23', '2027-03-02'])
        self.assertEqual([row['date'] for row in trial['not_offered']], ['02.02.2027', '09.02.2027'])

    def test_trials_closed_for_a_lesson_or_for_the_whole_studio(self):
        course, (lesson,) = self.course()
        lesson.trial_registration_open = False
        lesson.save(update_fields=['trial_registration_open'])
        form, trial = self.same(course)
        self.assertEqual(form['open'], set())
        self.assertEqual((trial['can_book'], trial['dates'], trial['open']), (False, [], False))
        self.assertEqual(trial['dates_text'], 'ההרשמה לשיעור ניסיון בחוג הזה סגורה כרגע')
        self.assertNotIn('not_offered', trial)

        lesson.trial_registration_open = None                       # back to the studio's rule…
        lesson.save(update_fields=['trial_registration_open'])
        policy = TrialRegistrationPolicy.current()
        policy.trials_open = False                                   # …which is now closed
        policy.save()
        form, trial = self.same(course)
        self.assertEqual((form['open'], trial['can_book']), (set(), False))

        lesson.trial_registration_open = True                        # opened by hand while the studio is closed
        lesson.save(update_fields=['trial_registration_open'])
        form, trial = self.same(course)
        self.assertEqual((len(form['open']), trial['can_book']), (3, True))

    def test_a_class_the_form_does_not_show(self):
        hidden, _lessons = self.course(show_in_widget=False)
        form, trial = self.same(hidden)
        self.assertEqual(form['open'], set())
        self.assertEqual((trial['can_book'], trial['dates']), (False, []))
        self.assertIn('לא פתוח להרשמה באתר', trial['dates_text'])

        switched_off = TestDataFactory.create_course_type('תחום שנסגר')
        switched_off.is_active = False
        switched_off.save(update_fields=['is_active'])
        course, _lessons = self.course('חוג של תחום שנסגר', course_type=switched_off)
        form, trial = self.same(course)
        self.assertEqual((form['open'], trial['can_book']), (set(), False))
        self.assertIn('החוג לא מוצג בהרשמה באתר', self.card(course)['note'])

    def test_a_class_with_a_link_of_its_own(self):
        course, _lessons = self.course(external_link='https://city.example/signup')
        form, trial = self.same(course)
        self.assertEqual((form['open'], trial['can_book'], trial['dates']), (set(), False, []))
        self.assertIn('בקישור של החוג', trial['dates_text'])

    def test_no_limit_set_anywhere(self):
        course, (lesson,) = self.course(capacity=0)
        self.room.capacity = 0                                       # zero is "not set", as on the course
        self.room.save(update_fields=['capacity'])
        form, trial = self.same(course)
        self.assertEqual(len(form['open']), 3)
        self.assertEqual([row['trial_seats_left'] for row in trial['dates']], [None, None, None])
        # The submit still stops at twenty in the room, though the picker shows the date as free.
        first = self.coming(lesson)[0]
        self.trials(lesson, first, 20)
        form, trial = self.same(course)
        self.assertEqual(form['full'], {(str(lesson.id), first.isoformat())})
        self.assertEqual([row['date'] for row in trial['dates']], ['09.02.2027', '16.02.2027'])

    def test_one_child_on_two_cards_holds_one_seat_on_the_picker_and_two_on_the_card(self):
        course, (lesson,) = self.course()
        lesson.capacity = 2
        lesson.save(update_fields=['capacity'])
        for _ in range(2):                                           # the same child, registered twice
            LessonEnrollment.objects.create(child=self.child(first_name='דנה', phone='0505550999'), lesson=lesson, status='active')
        form, trial = self.same(course)
        self.assertEqual((form['open'], trial['can_book']), (set(), False))   # the card counts two and shows "מלא"

    @override_settings(SUBSCRIPTION_FIRST_CHARGE_DATE='2027-02-10')
    def test_before_the_year_begins_the_dates_begin_with_it(self):
        course, _lessons = self.course()
        form, trial = self.same(course)
        self.assertEqual(sorted(day for _lesson, day in form['open']), ['2027-02-16', '2027-02-23', '2027-03-02'])
        self.assertNotIn('not_offered', trial)

    def test_the_form_has_no_last_day_of_the_year(self):
        course, _lessons = self.course()
        self.now = moment(date(2026, 12, 28))                        # a Monday; the dates run on into January
        form, trial = self.same(course)
        self.assertEqual(sorted(day for _lesson, day in form['open']), ['2026-12-29', '2027-01-05', '2027-01-12'])
        self.assertEqual(trial['dates_text'], 'אפשר להגיע לשיעור ניסיון ביום שלישי בשעה 17:00, בתאריכים 29.12 (מחר), 05.01 או 12.01')
        # Nor does it know when the season ends: at the end of July it offers August, unless the office closed those days.
        self.now = moment(date(2027, 7, 26))
        form, trial = self.same(course)
        self.assertEqual(sorted(day for _lesson, day in form['open']), ['2027-07-27', '2027-08-03', '2027-08-10'])
        TrialBlockedDate.objects.create(date=date(2027, 8, 3), reason='חופשת קיץ')
        TrialBlockedDate.objects.create(date=date(2027, 8, 10), reason='חופשת קיץ')
        form, trial = self.same(course)
        self.assertEqual([row['date'] for row in trial['dates']], ['27.07.2027', '17.08.2027', '24.08.2027'])
        self.assertEqual([row['why'] for row in trial['not_offered']], ['אין שיעורי ניסיון בתאריך הזה (חופשת קיץ)'] * 2)

    def test_a_lesson_held_once(self):
        course, (lesson,) = self.course()
        lesson.is_recurring = False
        lesson.lesson_date = date(2027, 2, 18)
        lesson.save(update_fields=['is_recurring', 'lesson_date'])
        form, trial = self.same(course)
        self.assertEqual(form['open'], {(str(lesson.id), '2027-02-18')})
        self.assertEqual(trial['dates_text'], 'אפשר להגיע לשיעור ניסיון ביום חמישי 18.02 בשעה 17:00')
        lesson.lesson_date = date(2027, 1, 28)                       # and one that has passed
        lesson.save(update_fields=['lesson_date'])
        form, trial = self.same(course)
        self.assertEqual((form['open'], trial['can_book'], trial['dates_text']), (set(), False, course_tools.NO_TRIAL_DATE))


class TodayTests(TrialOfferTestCase):
    """A lesson of today: Monday, 17:00–17:45."""

    def setUp(self):
        super().setUp()
        self.course_row, (self.lesson,) = self.course(days=((1, time(17, 0), time(17, 45)),))

    def test_before_it_begins_today_is_a_date(self):
        form, trial = self.same(self.course_row)
        self.assertIn((str(self.lesson.id), '2027-02-01'), form['open'])
        self.assertEqual((trial['dates'][0]['date'], trial['dates'][0]['when'], trial['dates'][0]['this_week']), ('01.02.2027', 'היום', True))
        self.assertEqual(trial['dates_text'], 'אפשר להגיע לשיעור ניסיון ביום שני בשעה 17:00, בתאריכים 01.02 (היום), 08.02 או 15.02')

    def test_once_it_has_begun_the_form_still_offers_it_and_the_bot_does_not(self):
        self.now = moment(hour=17, minute=20)
        form, trial = self.same(self.course_row, but_started=[(self.lesson, MONDAY)])
        self.assertIn((str(self.lesson.id), '2027-02-01'), form['open'])         # the form's own answer, until the lesson ends
        self.assertEqual([row['date'] for row in trial['dates']], ['08.02.2027', '15.02.2027'])
        self.assertEqual(trial['not_offered'], [{'date': '01.02.2027', 'day': 'שני', 'start': '17:00', 'why': 'השיעור של היום כבר התחיל'}])
        self.assertNotIn('01.02', trial['dates_text'])

    def test_after_it_ended_nobody_offers_it(self):
        self.now = moment(hour=18)
        form, trial = self.same(self.course_row)
        self.assertEqual(sorted(day for _lesson, day in form['open']), ['2027-02-08', '2027-02-15', '2027-02-22'])
        self.assertNotIn('not_offered', trial)
        self.assertFalse(any(row['this_week'] for row in trial['dates']))

    def test_the_week_is_sunday_to_saturday(self):
        self.now = moment(date(2027, 2, 6), hour=9)                 # Saturday
        _form, trial = self.same(self.course_row)
        self.assertEqual([(row['date'], row['this_week']) for row in trial['dates']][:2], [('08.02.2027', False), ('15.02.2027', False)])
        self.now = moment(date(2027, 2, 7), hour=9)                 # Sunday: the lesson of the 8th is tomorrow, and this week
        _form, trial = self.same(self.course_row)
        self.assertEqual((trial['dates'][0]['date'], trial['dates'][0]['when'], trial['dates'][0]['this_week']), ('08.02.2027', 'מחר', True))


class SeveralLessonsTests(TrialOfferTestCase):
    TWO_DAYS = ((0, time(16, 0), time(17, 0)), (3, time(16, 0), time(17, 0)))          # Sunday and Wednesday

    def test_a_class_of_two_days_offers_each_days_three_dates(self):
        course, (sunday, wednesday) = self.course(days=self.TWO_DAYS)
        form, trial = self.same(course)
        self.assertEqual(len(form['open']), 6)
        self.assertEqual([(row['day'], row['date']) for row in trial['dates']][:3], [('רביעי', '03.02.2027'), ('ראשון', '07.02.2027'), ('רביעי', '10.02.2027')])
        self.assertEqual(
            trial['dates_text'],
            'אפשר להגיע לשיעור ניסיון ביום רביעי 03.02 בשעה 16:00, ביום ראשון 07.02 בשעה 16:00 או ביום רביעי 10.02 בשעה 16:00',
        )
        # In a list of classes the nearest three are spelled out, and the card has them all.
        with clock_at(self.now):
            listed = run_tool('find_courses', {'city': 'ראש העין', 'course_type': 'קפוארה', 'show_all': True}, self.ctx())
        in_list = listed['courses'][0]['trial']
        self.assertEqual(([row['date'] for row in in_list['dates']], in_list['more_dates']), (['03.02.2027', '07.02.2027', '10.02.2027'], 3))
        self.assertEqual(in_list['dates_text'], trial['dates_text'])
        self.assertIn('trial.dates', listed['trial_dates_rule'])

    def test_one_day_closed_to_trials_leaves_the_other(self):
        course, (sunday, wednesday) = self.course(days=self.TWO_DAYS)
        sunday.trial_registration_open = False
        sunday.save(update_fields=['trial_registration_open'])
        form, trial = self.same(course)
        self.assertEqual({lesson for lesson, _day in form['open']}, {str(wednesday.id)})
        rows = {row['day']: row for row in self.card(course)['lessons']}
        self.assertEqual((rows['ראשון']['trial_status'], rows['ראשון']['trial_dates']), ('ההרשמה לניסיון סגורה', []))
        self.assertEqual((rows['רביעי']['trial_status'], len(rows['רביעי']['trial_dates'])), ('אפשר להירשם לניסיון', 3))

    def test_a_class_whose_every_day_is_required_offers_the_tracks_three_dates(self):
        course, lessons = self.course(days=self.TWO_DAYS, must_attend_all_lessons=True)
        self.track(course, lessons)
        form, trial = self.same(course)
        # Three dates in all, the earliest of the two days together — not three of each.
        self.assertEqual(sorted(day for _lesson, day in form['open']), ['2027-02-03', '2027-02-07', '2027-02-10'])
        self.assertEqual([row['date'] for row in trial['dates']], ['03.02.2027', '07.02.2027', '10.02.2027'])
        self.assertNotIn('more_dates', trial)

    def test_a_track_with_one_day_closed_to_trials(self):
        course, (sunday, wednesday) = self.course(days=self.TWO_DAYS, must_attend_all_lessons=True)
        self.track(course, [sunday, wednesday])
        wednesday.trial_registration_open = False
        wednesday.save(update_fields=['trial_registration_open'])
        form, trial = self.same(course)
        self.assertEqual(sorted(day for _lesson, day in form['open']), ['2027-02-07', '2027-02-14', '2027-02-21'])
        self.assertEqual({lesson for lesson, _day in form['open']}, {str(sunday.id)})

    def test_a_track_with_a_full_date_keeps_it_among_its_three(self):
        course, (sunday, wednesday) = self.course(days=self.TWO_DAYS, must_attend_all_lessons=True)
        self.track(course, [sunday, wednesday])
        self.pays(wednesday, 4)
        for day in self.coming(wednesday):
            self.trials(wednesday, day, 6)                           # every Wednesday is taken
        form, trial = self.same(course)
        # The picker still lists the Wednesdays, marked full, and with them only one Sunday fits in its three rows.
        self.assertEqual(form['open'], {(str(sunday.id), '2027-02-07')})
        self.assertEqual(form['full'], {(str(wednesday.id), '2027-02-03'), (str(wednesday.id), '2027-02-10')})
        self.assertEqual([row['date'] for row in trial['dates']], ['07.02.2027'])
        self.assertEqual(trial['dates_text'], 'אפשר להגיע לשיעור ניסיון ביום ראשון 07.02 בשעה 16:00')

    def test_a_track_with_one_day_full_offers_nothing(self):
        course, (sunday, wednesday) = self.course(days=self.TWO_DAYS, must_attend_all_lessons=True)
        self.track(course, [sunday, wednesday])
        self.pays(sunday, 10)
        form, trial = self.same(course)
        self.assertEqual((form['open'], trial['can_book']), (set(), False))
        card = self.card(course)
        self.assertEqual(card['tracks'][0]['is_full'], True)
        self.assertIn('החוג מלא כרגע', trial['dates_text'])
        self.assertEqual(
            {row['day']: (row['is_full'], row['trial_status']) for row in card['lessons']},
            {'ראשון': (True, 'המועד מלא'), 'רביעי': (False, 'המסלול מלא (אחד מהימים שלו מלא)')},
        )

    def test_a_class_with_single_days_and_a_track(self):
        course, lessons = self.course(days=self.TWO_DAYS)
        self.track(course, lessons)
        form, _trial = self.same(course)
        self.assertEqual(len(form['open']), 6)                       # each day's own three; the track adds none

    def test_a_class_whose_every_day_is_required_and_has_no_track(self):
        course, lessons = self.course(days=self.TWO_DAYS, must_attend_all_lessons=True)
        form, trial = self.same(course)
        self.assertEqual((form['open'], trial['can_book']), (set(), False))
        self.assertIn('אין כרגע מועדים באתר', trial['dates_text'])
        # The form has no row for such a class, and so a search does not offer it either.
        asked = {'city': 'ראש העין', 'course_type': 'קפוארה', 'show_all': True}
        self.assertEqual(run_tool('find_courses', asked, self.ctx())['courses'], [])
        self.track(course, lessons)
        self.assertEqual([row['name'] for row in run_tool('find_courses', asked, self.ctx())['courses']], [course.name])

    def test_the_instructors_track_shows_no_single_day(self):
        course, lessons = self.course(days=self.TWO_DAYS, min_age=INSTRUCTORS_AGE_GROUP, max_age=INSTRUCTORS_AGE_GROUP)
        form, trial = self.same(course)
        self.assertEqual((form['open'], trial['can_book']), (set(), False))
        self.track(course, lessons)
        form, trial = self.same(course)
        self.assertEqual(len(form['open']), 3)


class ToolAnswerTests(TrialOfferTestCase):
    def test_the_clock_of_the_conversation_moves_the_dates(self):
        course, _lessons = self.course()
        self.assertEqual(self.card(course)['trial']['dates'][0]['date'], '02.02.2027')
        self.now = moment(date(2027, 5, 10))
        self.assertEqual(self.card(course)['trial']['dates'][0]['date'], '11.05.2027')
        # With no clock of its own a conversation is answered on the real one, as the form answers.
        real = run_tool('course_card', {'course_id': str(course.id)}, ToolContext(None))['course']['trial']['dates']
        lesson = course.lessons.get()
        ahead = [day.strftime('%d.%m.%Y') for day in iter_upcoming_lesson_occurrences(lesson, count=3)]
        self.assertEqual([row['date'] for row in real][-2:], ahead[-2:])     # (the first may be a lesson of today that has begun)

    def test_the_widgets_own_counts_do_not_change_when_no_clock_is_handed(self):
        from apps.customers.widget_views import _lesson_widget_capacity, trial_seats_by_date

        course, (lesson,) = self.course()
        self.pays(lesson, 3)
        self.trials(lesson, iter_upcoming_lesson_occurrences(lesson, count=1)[0], 2)
        seats = trial_offer.Seats([course_tools.with_lessons(type(course).objects.filter(id=course.id)).get()])
        plain = _lesson_widget_capacity(lesson, course, seats.enrolled, seats.trials, seats.occurrences)
        self.assertEqual(plain, _lesson_widget_capacity(lesson, course, seats.enrolled, seats.trials, seats.occurrences, now=None))
        self.assertEqual(plain, _lesson_widget_capacity(lesson, course, seats.enrolled, seats.trials, seats.occurrences, now=timezone.localtime()))
        by_date = trial_seats_by_date(lesson, 10, 3, seats.trials.get(lesson.id, {}))
        self.assertEqual(by_date, trial_seats_by_date(lesson, 10, 3, seats.trials.get(lesson.id, {}), now=timezone.localtime()))
        self.assertEqual([left for _day, left in by_date], [5, 7, 7])

    def test_a_family_the_form_would_refuse_is_told_so(self):
        course, (lesson,) = self.course()
        had_one = self.child(status='trial_completed', first_name='יואב')
        pays_here = self.child(first_name='מיכל')
        LessonEnrollment.objects.create(child=pays_here, lesson=lesson, status='active')
        new = self.child(status='pending', first_name='שחר')
        contact = Contact.objects.create(phone=PHONE, name='רותם', kogo_child_ids=[str(had_one.id), str(pays_here.id), str(new.id)])
        trial = self.card(course, self.ctx(contact))['trial']
        self.assertEqual(len(trial['for_this_customer']), 2)
        self.assertIn('ליואב כבר היה שיעור ניסיון', trial['for_this_customer'][0])
        self.assertIn('מיכל כבר רשום לחוג הזה', trial['for_this_customer'][1])
        self.assertNotIn('for_this_customer', self.card(course)['trial'])           # nobody is identified: nothing is said

    def test_reading_the_dates_writes_nothing(self):
        course, lessons = self.course(days=SeveralLessonsTests.TWO_DAYS)
        self.track(course, lessons)
        self.trials(lessons[0], self.coming(lessons[0])[0], 2)
        verbs = set()

        def watching(execute, sql, params, many, context):
            verbs.add(sql.lstrip().split(None, 1)[0].upper())
            return execute(sql, params, many, context)

        with connection.execute_wrapper(watching):
            self.card(course)
            run_tool('find_courses', {'city': 'ראש העין', 'show_all': True}, self.ctx())
        self.assertEqual(verbs - {'SELECT', 'SAVEPOINT', 'RELEASE'}, set())

    def test_the_tools_say_where_a_trial_date_comes_from(self):
        from apps.wahub import shadow_tools

        descriptions = {tool['name']: tool['description'] for tool in shadow_tools.TOOLS}
        self.assertIn('עונים רק מ-trial.dates של החוג', descriptions['find_courses'])
        self.assertIn('תאריך לניסיון נאמר רק מתוך dates', descriptions['course_card'])
        course, _lessons = self.course()
        result = run_tool('course_card', {'course_id': str(course.id)}, self.ctx())
        self.assertEqual(result['trial_dates_rule'], course_tools.TRIAL_DATES_RULE)
        self.assertIn('לא מחשבים תאריך לבד', result['trial_dates_rule'])
        self.assertNotIn('next_dates', str(result))
