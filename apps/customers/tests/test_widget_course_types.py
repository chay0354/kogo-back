from django.test import SimpleTestCase

from apps.customers.widget_course_types import sort_widget_course_types, widget_course_type_rank


class WidgetCourseTypeOrderTest(SimpleTestCase):
    def test_pinned_order_puts_capoeira_first(self):
        types = [
            {'id': '4', 'name': 'ברייקדאנס'},
            {'id': '3', 'name': 'אקרובטיקה אווירית'},
            {'id': '2', 'name': 'מחול'},
            {'id': '1', 'name': 'קפוארה'},
        ]
        names = [row['name'] for row in sort_widget_course_types(types)]
        self.assertEqual(names, ['קפוארה', 'מחול', 'אקרובטיקה אווירית', 'ברייקדאנס'])

    def test_missing_types_are_skipped_and_others_follow(self):
        types = [
            {'id': 'z', 'name': 'יוגה'},
            {'id': 'a', 'name': 'אקרובטיקה אווירית'},
            {'id': 'c', 'name': 'קפואירה'},
        ]
        names = [row['name'] for row in sort_widget_course_types(types)]
        self.assertEqual(names, ['קפואירה', 'אקרובטיקה אווירית', 'יוגה'])

    def test_capoeira_spelling_variants_share_first_rank(self):
        self.assertEqual(widget_course_type_rank('קפוארה'), 0)
        self.assertEqual(widget_course_type_rank('קפואירה'), 0)
