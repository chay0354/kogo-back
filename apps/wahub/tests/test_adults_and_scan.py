"""
"יש קבוצת בוגרים במינץ" — the owner, 14.10.2026, 08:23, after the bot told him there is none.

The adults' group of מינץ is not a class of its own: it is the track "קבוצת בוגרים"
(its own age group: בוגרים) of the instructors' capoeira class. The bot read only the
class's own group (מדריכים) and dropped it. What is held here:

  * a class is open to a child the way the registration form opens it
    (kogo-front catalogRows.ts): its own days by its own age group — never the
    instructors' days — and a track or a price of a day by its own group when it has one;
  * such a class is told as it is open to that child: the track that takes him, no
    price of the class, and a note that says how he joins;
  * with the field and the age known and no place, every branch is looked at
    (the owner: "לסרוק את כל השיעורים במערכת ולבדוק איפה יש"), and a place with
    nothing says where else there is.
"""
from datetime import time
from decimal import Decimal

from apps.core.tests.test_fixtures import TestDataFactory
from apps.courses.models import LessonBundle, LessonPriceOption
from apps.wahub import course_tools, shadow_tools
from apps.wahub.tests.test_course_tools import CourseToolsTestCase, _without_ids


class AdultsTrackTestCase(CourseToolsTestCase):
    def setUp(self):
        super().setUp()
        self.petah_tikva = TestDataFactory.create_city('פתח תקווה')
        self.mintz = TestDataFactory.create_branch(name='מרכז העיר - מינץ 24', city=self.petah_tikva, address='מינץ 24')
        # The instructors' class (position 16 at both ends), open to all its lessons at ₪150 —
        # and the adults' group, a track of its own age group, at ₪330.
        self.instructors = TestDataFactory.create_course(
            name='קפוארה ואקרובטיקה מדריכים - פתוח לכל השיעורים', branch=self.mintz, course_type=self.capoeira,
            price=Decimal('150.00'), min_age=16, max_age=16,
        )
        self.monday = TestDataFactory.create_lesson(
            course=self.instructors, day_of_week=1, start_time=time(20, 15), end_time=time(21, 45),
        )
        self.thursday = TestDataFactory.create_lesson(
            course=self.instructors, day_of_week=4, start_time=time(20, 15), end_time=time(21, 45),
        )
        self.all_lessons = LessonBundle.objects.create(
            course=self.instructors, name='', combined_price=Decimal('150.00'), min_age=16, max_age=16,
        )
        self.all_lessons.lessons.set([self.monday, self.thursday])
        self.adults = LessonBundle.objects.create(
            course=self.instructors, name='קבוצת בוגרים', combined_price=Decimal('330.00'), min_age=15, max_age=15,
        )
        self.adults.lessons.set([self.monday, self.thursday])

    def find_anywhere(self, **arguments):
        return self.tool('find_courses', **arguments)


class TheAdultsTrackIsFoundTest(AdultsTrackTestCase):
    def test_the_adults_group_of_mintz_is_found_and_told_as_its_track(self):
        result = self.tool('find_courses', city='פתח תקווה', course_type='קפוארה', grade='בוגרים')
        self.assertEqual([row['name'] for row in result['courses']], [self.instructors.name])
        course = result['courses'][0]
        # Only the track that takes an adult, with its own price; not the class's ₪150, nor the instructors' track.
        self.assertEqual([row['name'] for row in course['tracks']], ['קבוצת בוגרים'])
        self.assertEqual(course['tracks'][0]['price_monthly'], '330.00')
        self.assertEqual(course['tracks'][0]['ages'], 'בוגרים')
        self.assertEqual(course['tracks'][0]['lessons'], ['שני 20:15–21:45', 'חמישי 20:15–21:45'])
        self.assertIsNone(course['price_monthly'])
        self.assertEqual(course['join_note'], course_tools.JOIN_THROUGH_TRACK)
        self.assertNotIn('150.00', _without_ids(course))

    def test_the_form_rule_for_each_kind_of_row(self):
        adult, child = {15}, {5}
        self.assertEqual(course_tools.age_offer(self.instructors, adult), {'days': False, 'tracks': [str(self.adults.id)], 'options': []})
        self.assertIsNone(course_tools.age_offer(self.instructors, child))
        # The instructors' own days are never on the form one at a time; its track of two days is, for an instructor.
        offer = course_tools.age_offer(self.instructors, {16})
        self.assertEqual((offer['days'], offer['tracks']), (False, [str(self.all_lessons.id)]))
        # A track with no age group of its own follows the class's; a class's own days follow its own.
        self.assertEqual(course_tools.age_offer(self.older, {5}), {'days': True, 'tracks': [str(self.track.id)], 'options': []})
        self.assertIsNone(course_tools.age_offer(self.older, {15}))

    def test_a_price_of_a_day_with_an_age_group_of_its_own(self):
        option = LessonPriceOption.objects.create(
            lesson=self.older_sunday, display_title='קפוארה להורים', monthly_price=Decimal('210.00'), min_age=15, max_age=15,
        )
        result = self.find(course_type='קפוארה', grade='בוגרים')
        course = next(row for row in result['courses'] if row['name'] == self.older.name)
        self.assertIsNone(course['price_monthly'])
        self.assertEqual(course['join_note'], course_tools.JOIN_THROUGH_OPTION)
        sunday = next(row for row in course['lessons'] if row['day'] == 'ראשון')
        self.assertEqual(sunday['price_options'], [{'id': str(option.id), 'title': 'קפוארה להורים', 'price_monthly': '210.00', 'ages': 'בוגרים'}])
        self.assertEqual(course['tracks'], [])

    def test_a_child_is_still_told_the_class_as_before(self):
        course = self.find(course_type='קפוארה', age=9)['courses'][0]
        self.assertEqual((course['name'], course['price_monthly']), (self.older.name, '260.00'))
        self.assertNotIn('join_note', course)
        self.assertEqual([row['name'] for row in course['tracks']], ['פעמיים בשבוע'])

    def test_the_quote_reads_the_tracks_own_age_group(self):
        quoted = self.quote(self.instructors, track_id=str(self.adults.id), grade='בוגרים')
        self.assertTrue(quoted['can_quote'])
        self.assertFalse([note for note in quoted['notes'] if 'לא בטווח' in note])
        refused = self.quote(self.instructors, track_id=str(self.adults.id), grade='כיתה ג')
        self.assertIn('המסלול מיועד לבוגרים — הגיל שנמסר לא בטווח', refused['notes'])


class EveryBranchIsLookedAtTest(AdultsTrackTestCase):
    def test_no_place_said_one_branch_holds_it(self):
        result = self.find_anywhere(course_type='קפוארה', grade='בוגרים')
        self.assertTrue(result['scanned_all'])
        self.assertEqual(result['need'], '')
        self.assertEqual([row['name'] for row in result['branches']], [self.mintz.name])
        self.assertEqual([row['name'] for row in result['courses']], [self.instructors.name])
        self.assertIn(shadow_tools.SCAN_ONE, result['hint'])

    def test_no_place_said_and_no_branch_holds_it(self):
        result = self.find_anywhere(course_type='היפהופ - מחול', grade='בוגרים')
        self.assertEqual((result['need'], result['courses'], result['hint']), ('', [], shadow_tools.SCAN_NOWHERE))
        self.assertTrue(result['scanned_all'])

    def test_no_place_said_and_a_few_branches_hold_it(self):
        TestDataFactory.create_course(
            name='קפוארה בוגרים', branch=TestDataFactory.create_branch(name='שוהם - המלכה 27', city=TestDataFactory.create_city('שוהם')),
            course_type=self.capoeira, price=Decimal('280.00'), min_age=15, max_age=15,
        )
        TestDataFactory.create_lesson(course=self.instructors.__class__.objects.get(name='קפוארה בוגרים'), day_of_week=2)
        result = self.find_anywhere(course_type='קפוארה', grade='בוגרים')
        self.assertEqual(result['need'], 'branch')
        self.assertEqual(sorted(row['name'] for row in result['branches']), ['מרכז העיר - מינץ 24', 'שוהם - המלכה 27'])
        self.assertEqual(result['courses'], [])
        self.assertIn(shadow_tools.SCAN_FEW, result['hint'])

    def test_no_place_said_and_many_branches_hold_it_asks_the_city(self):
        for index in range(5):
            branch = TestDataFactory.create_branch(name=f'סניף בדוי {index}', city=TestDataFactory.create_city(f'עיר בדויה {index}'))
            course = TestDataFactory.create_course(
                name=f'קפוארה 4.5-6 בדוי {index}', branch=branch, course_type=self.capoeira, price=Decimal('260.00'), min_age=2, max_age=2,
            )
            TestDataFactory.create_lesson(course=course, day_of_week=index)
        result = self.find_anywhere(course_type='קפוארה', age=5)
        self.assertEqual(result['need'], 'city')
        self.assertIn('ראש העין', result['cities'])
        self.assertNotIn('רמת גן', result['cities'])       # the municipality's branch holds nothing of ours
        self.assertIn(shadow_tools.SCAN_MANY, result['hint'])

    def test_without_the_field_or_the_age_the_city_is_asked_as_before(self):
        for arguments in ({'course_type': 'קפוארה'}, {'grade': 'בוגרים'}, {}):
            result = self.find_anywhere(**arguments)
            self.assertEqual(result['need'], 'city', arguments)
            self.assertNotIn('scanned_all', result)

    def test_a_place_with_nothing_says_where_else_there_is(self):
        result = self.find(course_type='קפוארה', grade='בוגרים')          # ראש העין has no adults' group
        self.assertEqual(result['courses'], [])
        self.assertEqual(result['elsewhere'], [{'branch': 'מרכז העיר - מינץ 24', 'city': 'פתח תקווה', 'classes': [f'{self.instructors.name} — קבוצת בוגרים']}])
        self.assertIn(shadow_tools.ELSEWHERE, result['hint'])
        # Nothing elsewhere either: no such key.
        self.assertNotIn('elsewhere', self.find(course_type='היפהופ - מחול', grade='בוגרים'))


class TheTeachChatLooksAtTheSystemTest(AdultsTrackTestCase):
    """"ללמד את הבוט" is shown what Kogo holds about what the owner wrote, and what the bot's search returns."""

    def test_what_the_owner_wrote_is_read_for_a_field_a_place_and_an_age(self):
        from apps.wahub import system_look

        self.assertEqual(system_look.asked_of('יש קבוצת בוגרים בקפוארה במינץ')['field'], 'קפוארה')
        asked = system_look.asked_of('יש קבוצת בוגרים בקפוארה במינץ')
        self.assertEqual((asked['place'], asked['grade'], asked['codes']), ('מינץ', 'בוגרים', {15}))
        self.assertEqual(system_look.asked_of('לענות בעברית ולא באנגלית'), None)
        self.assertEqual(system_look.asked_of('יש קפוארה לבן 5 בראש העין')['age'], 5.0)

    def test_the_adults_group_is_shown_with_its_track_and_the_bot_finds_it(self):
        from apps.wahub import system_look

        found = system_look.look('יש קבוצת בוגרים בקפוארה במינץ, הבוט אמר שאין')
        self.assertEqual(len(found['lines']), 1)
        self.assertIn('"קפוארה ואקרובטיקה מדריכים - פתוח לכל השיעורים"', found['lines'][0])
        self.assertIn('דרך המסלול "קבוצת בוגרים" (בוגרים; שני 20:15, חמישי 20:15)', found['lines'][0])
        self.assertEqual(found['bot'], f'מצא: {self.instructors.name}')
        self.assertFalse(found['gap'])
        text = system_look.section(found)
        self.assertIn('# מה יש במערכת Kogo עכשיו (קפוארה, מינץ, בוגרים)', text)
        self.assertNotIn('330', text)
        self.assertNotIn('150', text)

    def test_a_class_the_search_misses_is_a_gap_and_is_logged(self):
        from unittest.mock import patch as patched

        from apps.wahub import system_look

        with patched('apps.wahub.shadow_tools.find_courses', return_value={'courses': [], 'need': '', 'branches': []}):
            with self.assertLogs('apps.wahub.system_look', level='WARNING') as logged:
                found = system_look.look('יש קבוצת בוגרים בקפוארה במינץ')
        self.assertTrue(found['gap'])
        self.assertIn('the bot\'s search does not find them', logged.output[0])
        self.assertIn('תקלה בחיפוש', system_look.section(found))

    def test_a_class_hidden_from_the_form_is_said_as_hidden(self):
        from apps.wahub import system_look

        self.instructors.show_in_widget = False
        self.instructors.save(update_fields=['show_in_widget'])
        found = system_look.look('יש קבוצת בוגרים בקפוארה במינץ')
        self.assertIn(system_look.HIDDEN, found['lines'][0])
        self.assertFalse(found['gap'])

    def test_the_teach_chat_is_shown_the_look(self):
        from apps.wahub import refine

        self.assertIn('עובדה על אילו חוגים, קבוצות, ימים או שעות יש — לא כותבים בידע אף פעם', refine.TEACH_SYSTEM)
        thread = refine.RefineThread.objects.create()
        turn = refine.RefineTurn.objects.create(thread=thread, role=refine.TURN_OPERATOR, text='יש קבוצת בוגרים בקפוארה במינץ')
        told = refine._teach_user_text(thread, [turn], [])
        self.assertIn('# מה יש במערכת Kogo עכשיו', told)
        self.assertIn('החיפוש של הבוט על אותה שאלה, עכשיו: מצא:', told)
        plain = refine.RefineTurn.objects.create(thread=thread, role=refine.TURN_OPERATOR, text='לענות תמיד בעברית')
        self.assertNotIn('מה יש במערכת', refine._teach_user_text(thread, [plain], []))
