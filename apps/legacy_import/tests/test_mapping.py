"""Where a location from the old software is suggested to go — against kogo's own names."""
from django.test import SimpleTestCase

from apps.legacy_import.mapping import Option, place_tokens, suggest

BUSINESSES = [
    Option('lessons', 'חוגים'),
    Option('brand', 'מותג קוגומלו'),
    Option('shows', 'הצגות חיצוניות'),
]
CATEGORIES = [
    Option('branches', 'סניפים', business_id='lessons'),
    Option('merch', 'מרצנדייס משלוחים', business_id='brand'),
    Option('brand-general', 'כללי', business_id='brand'),
    Option('shows-general', 'כללי', business_id='shows'),
]
BRANCHES = [
    Option('zamir', 'מרכז זמיר'),
    Option('kfar-saba', 'כפר סבא'),
    Option('or-yehuda', 'אור יהודה'),
    Option('yehud', 'יהוד'),
    Option('ramat-gan', 'רמת גן'),
    Option('amishav', 'עמישב פתח תקווה'),
    Option('closed', 'שוהם', is_active=False),
]


def run(location, details=(), businesses=BUSINESSES, categories=CATEGORIES, branches=BRANCHES):
    return suggest(location, list(details), businesses, categories, branches)


class PlaceTokenTests(SimpleTestCase):
    def test_generic_words_quotes_and_abbreviations(self):
        self.assertEqual(place_tokens('סניף מתנ"ס עמישב פ"ת'), ['עמישב', 'פתח', 'תקווה'])
        self.assertEqual(place_tokens('מתנ״ס אור יהודה'), ['אור', 'יהודה'])
        self.assertEqual(place_tokens('סניף קאנטרי הספורטן'), ['הספורטן'])


class BranchSuggestionTests(SimpleTestCase):
    def test_a_branch_by_its_name_under_the_branches_category(self):
        result = run('כפר סבא')
        self.assertEqual(
            (result['kind'], result['business_id'], result['category_id'], result['branch_id']),
            ('branch', 'lessons', 'branches', 'kfar-saba'),
        )

    def test_the_community_centre_and_the_town_are_the_same_branch(self):
        self.assertEqual(run('מתנס אור יהודה')['branch_id'], 'or-yehuda')
        self.assertEqual(run('אור יהודה')['branch_id'], 'or-yehuda')

    def test_abbreviated_city_meets_the_spelled_out_branch(self):
        self.assertEqual(run('סניף מתנ"ס עמישב פ"ת')['branch_id'], 'amishav')

    def test_a_shared_word_is_not_a_match(self):
        # יהוד is not אור יהודה, and רמת גן is not every place with גן in it.
        self.assertEqual(run('יהוד')['branch_id'], 'yehud')
        self.assertIsNone(run('מתנס נווה גן')['branch_id'])

    def test_the_place_named_in_the_documents_details(self):
        details = ['הדרכת קפוארה מתנס זמיר כפג'] * 3 + ['חוג']
        result = run('סניף כפר גנים ג פ"ת', details)
        self.assertEqual(result['branch_id'], 'zamir')
        self.assertIn('פרטי', result['reason'])

    def test_a_place_mentioned_once_is_not_enough(self):
        self.assertIsNone(run('סניף כפר גנים ג פ"ת', ['מתנס זמיר'])['branch_id'])

    def test_a_closed_branch_is_not_suggested(self):
        self.assertIsNone(run('שוהם')['branch_id'])

    def test_two_branches_that_fit_equally_is_left_to_the_owner(self):
        branches = [Option('a', 'גבעת כוח צפון'), Option('b', 'גבעת כוח דרום')]
        self.assertIsNone(run('גבעת כוח', branches=branches)['branch_id'])

    def test_without_a_branches_category_the_branch_alone(self):
        result = run('כפר סבא', categories=[c for c in CATEGORIES if c.name != 'סניפים'])
        self.assertEqual((result['branch_id'], result['business_id'], result['category_id']), ('kfar-saba', None, None))


class SectionSuggestionTests(SimpleTestCase):
    def test_merch_goes_to_the_merch_category(self):
        result = run('22 מרצנדייס')
        self.assertEqual((result['kind'], result['business_id'], result['category_id']), ('section', 'brand', 'merch'))

    def test_shows_go_to_the_shows_business(self):
        result = run('21 הצגות מופעים ופסטיבלים במותג')
        self.assertEqual((result['business_id'], result['category_id']), ('shows', 'shows-general'))

    def test_the_brand_is_its_general_category_not_merch(self):
        result = run('20 כללי מותג  - הכנסות/הוצאות עבור תוכן שיווקי')
        self.assertEqual((result['business_id'], result['category_id']), ('brand', 'brand-general'))

    def test_expenses_are_flagged_and_left_unmapped(self):
        for location in ('החזר הוצאות שוקיו', '10 החזר הוצאות - רהע', '01 הוצאות חברה כללי - (סעיף 1)',
                         '02 הכנסות/הוצאות כלליות של סניפים ומרכזים (סעיף 2) - החרגה רהע'):
            with self.subTest(location):
                result = run(location)
                self.assertEqual(result['kind'], 'expenses')
                self.assertTrue(result['flag'])
                self.assertEqual((result['business_id'], result['category_id'], result['branch_id']), (None, None, None))

    def test_a_section_with_no_keyword_is_for_the_owner(self):
        result = run('23 כורוגרפיות קפואריסטים ורקדניות')
        self.assertEqual((result['kind'], result['business_id']), ('section', None))

    def test_a_keyword_business_that_does_not_exist_says_so(self):
        result = run('21 הצגות', businesses=[b for b in BUSINESSES if b.id != 'shows'])
        self.assertIsNone(result['business_id'])
        self.assertIn('בחרו ידנית', result['reason'])
