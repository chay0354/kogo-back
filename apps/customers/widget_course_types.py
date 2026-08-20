"""Widget course-type order — same in every branch's 'בחרו חוג' list."""

# First match wins. Names are matched as substrings after light normalization.
_WIDGET_COURSE_TYPE_PRIORITY = (
    ('קפואירה', 'קפוארה', 'capoeira'),
    ('מחול', 'היפ-הופ', 'היפהופ', 'hip-hop', 'hiphop'),
    ('אקרובטיקה אווירית', 'אווירית', 'aerial'),
    ('ברייקדאנס', 'ברייק דאנס', 'ברייק', 'breakdance', 'break dance'),
)


def _normalize_type_name(name: str) -> str:
    return ' '.join((name or '').strip().lower().replace('-', ' ').split())


def widget_course_type_rank(name: str) -> int:
    normalized = _normalize_type_name(name)
    if not normalized:
        return len(_WIDGET_COURSE_TYPE_PRIORITY)
    for index, keywords in enumerate(_WIDGET_COURSE_TYPE_PRIORITY):
        if any(_normalize_type_name(keyword) in normalized for keyword in keywords):
            return index
    return len(_WIDGET_COURSE_TYPE_PRIORITY)


def sort_widget_course_types(types: list[dict]) -> list[dict]:
    """Stable order: pinned types first, then remaining names alphabetically."""
    return sorted(
        types,
        key=lambda item: (widget_course_type_rank(item.get('name') or ''), item.get('name') or ''),
    )
