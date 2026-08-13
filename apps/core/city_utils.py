"""Shared city name normalization and deduplication (widget + CRM parity)."""


def normalize_city_name(name: str) -> str:
    return ' '.join((name or '').strip().split())


def dedupe_cities_by_name(cities: list[dict]) -> list[dict]:
    """Keep one city per normalized name (first occurrence wins)."""
    by_name: dict[str, dict] = {}
    for city in cities:
        key = normalize_city_name(city.get('name', '')).lower()
        if key and key not in by_name:
            by_name[key] = city
    return sorted(by_name.values(), key=lambda c: c.get('name', ''))


def build_city_id_alias_map(cities: list[dict]) -> dict[str, str]:
    """
    Map every city id to the canonical id for its normalized name.
    Used so branches linked to duplicate city rows still filter correctly.
    """
    canonical_by_name: dict[str, str] = {}
    for city in cities:
        key = normalize_city_name(city.get('name', '')).lower()
        if key and key not in canonical_by_name:
            canonical_by_name[key] = str(city['id'])

    aliases: dict[str, str] = {}
    for city in cities:
        key = normalize_city_name(city.get('name', '')).lower()
        canonical = canonical_by_name.get(key)
        if canonical:
            aliases[str(city['id'])] = canonical
    return aliases
