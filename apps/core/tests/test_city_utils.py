from django.test import TestCase

from apps.core.city_utils import build_city_id_alias_map, dedupe_cities_by_name


class CityUtilsTests(TestCase):
    def test_dedupe_cities_by_name(self):
        cities = [
            {'id': '1', 'name': 'רמלה'},
            {'id': '2', 'name': ' רמלה '},
            {'id': '3', 'name': 'פתח תקווה'},
        ]
        deduped = dedupe_cities_by_name(cities)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0]['name'], 'פתח תקווה')
        self.assertEqual(deduped[1]['name'], 'רמלה')

    def test_build_city_id_alias_map(self):
        cities = [
            {'id': 'keep', 'name': 'רמלה'},
            {'id': 'dup', 'name': 'רמלה'},
        ]
        aliases = build_city_id_alias_map(cities)
        self.assertEqual(aliases['keep'], 'keep')
        self.assertEqual(aliases['dup'], 'keep')
