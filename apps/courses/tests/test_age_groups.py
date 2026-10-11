"""
The age-group scale: `Course.min_age` / `max_age` are positions on it, not years.

The labels are the ones the CRM and the widget show (kogo-front
`src/lib/courseUtils.ts`, AGE_GROUP_LABELS). What is held here is the reading
of a child's age in years onto that scale — the step whose absence sent a
five-year-old to the group of grades ג–ד.
"""
from django.test import SimpleTestCase

from apps.courses import age_groups


class AgeGroupScaleTest(SimpleTestCase):
    def test_the_scale_is_the_one_the_screens_show(self):
        self.assertEqual(len(age_groups.AGE_GROUP_LABELS), 16)
        self.assertEqual(age_groups.AGE_GROUP_LABELS[:3], ('3-4.5', '4.5-6', 'כיתה א'))
        self.assertEqual(age_groups.AGE_GROUP_LABELS[13:], ('כיתה י״ב', 'בוגרים', 'מדריכים'))

    def test_an_age_in_years_is_a_position_and_never_itself(self):
        self.assertEqual(age_groups.codes_for_age(5), {2})                # "4.5-6" — not position 5, which is כיתה ג
        self.assertEqual(age_groups.codes_for_age(3), {1})
        self.assertEqual(age_groups.codes_for_age(9), {5, 6})             # grade ג or ד
        self.assertEqual(age_groups.codes_for_age(30), {15})
        self.assertEqual(age_groups.codes_for_age(2.5), set())            # no group under three
        self.assertEqual(age_groups.codes_for_age('abc'), set())

    def test_an_age_on_the_line_belongs_to_both_sides(self):
        self.assertEqual(age_groups.codes_for_age(4.5), {1, 2})
        self.assertEqual(age_groups.codes_for_age(6), {2, 3})             # גן חובה or כיתה א
        self.assertEqual(age_groups.codes_for_age(18), {14, 15})

    def test_a_grade_as_a_parent_writes_it(self):
        self.assertEqual(age_groups.codes_for_grade('כיתה ג'), {5})
        self.assertEqual(age_groups.codes_for_grade("ג'"), {5})
        self.assertEqual(age_groups.codes_for_grade('י״א'), {13})
        self.assertEqual(age_groups.codes_for_grade('יב'), {14})
        self.assertEqual(age_groups.codes_for_grade('כיתה 3'), {5})
        self.assertEqual(age_groups.codes_for_grade('גן חובה'), {2})
        self.assertEqual(age_groups.codes_for_grade('גן'), {1, 2})
        self.assertEqual(age_groups.codes_for_grade('בוגרים'), {15})
        self.assertEqual(age_groups.codes_for_grade('לא יודעת'), set())

    def test_whether_a_course_takes_a_child(self):
        five = age_groups.codes_for_age(5)
        self.assertTrue(age_groups.fits(2, 2, five))
        self.assertFalse(age_groups.fits(5, 6, five))                     # grades ג–ד do not take a five-year-old
        self.assertTrue(age_groups.fits(None, None, five))                # no range set: open
        self.assertTrue(age_groups.fits(1, None, five))
        self.assertFalse(age_groups.fits(2, 2, set()))

    def test_a_range_in_words_and_in_years(self):
        self.assertEqual(age_groups.range_label(1, 1), 'גיל 3-4.5')
        self.assertEqual(age_groups.range_label(1, 2), 'גיל 3-6')
        self.assertEqual(age_groups.range_label(3, 4), 'כיתות א–ב')
        self.assertEqual(age_groups.range_label(9, 14), 'כיתות ז–י״ב')
        self.assertEqual(age_groups.range_label(2, 4), 'גיל 4.5-6 עד כיתה ב')
        self.assertEqual(age_groups.range_label(15, 15), 'בוגרים')
        self.assertEqual(age_groups.range_label(None, None), '')
        self.assertEqual(age_groups.years_range(2, 2), (4.5, 6.0))
        self.assertEqual(age_groups.years_range(5, 6), (8.0, 10.0))
        self.assertEqual(age_groups.years_range(15, 15), (18.0, None))
        self.assertEqual(age_groups.years_range(None, None), (None, None))
