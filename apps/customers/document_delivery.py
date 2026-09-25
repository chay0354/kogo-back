"""Where each issued document's signed original went — for the cards that list documents.

Every original kogo signs is one SignedOriginal row (apps/documents/models.py),
keyed by the issuing row: kind 'ir' (Invoice), 'store' (StoreInvoice) or
'formal' (FormalDocument) plus that row's id as source_id — and, one row per
fiscal number, by the number too. A card that lists documents asks here, once
for the whole list, how each one reached the customer: mailed, on the list to
hand over on paper (and whether that one print was made), held, or an archive
copy of a document issued before signing existed. A document with no row at
all — issued before signing, not yet copied to the archive — reads None.

Read only, and never the stored bytes: the query names its columns.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from django.db.models import Q
from django.utils import timezone

from apps.documents.models import SignedOriginal

# Everything the cards show, and what the lookup matches on — never `pdf`.
_FIELDS = (
    'id', 'number', 'kind', 'source_id', 'purpose', 'delivery', 'delivery_reason',
    'sent_at', 'paper_original_printed_at', 'signed_at',
)


def israel_moment(value) -> str | None:
    """
    A moment as ISO text on Israel's clock, with its offset — as DRF writes the
    customer's own fields — so a screen that reads the day off the text reads
    the office's day, and one that converts gets the same moment.
    """
    if value is None:
        return None
    return timezone.localtime(value).isoformat() if timezone.is_aware(value) else value.isoformat()


def delivery_status(row: SignedOriginal) -> dict:
    """
    One stored original as the cards show it.

    purpose is 'archive' for an archive copy and 'original' otherwise — a NULL
    written by an earlier deployment is an original, as every reader treats it.
    """
    return {
        'delivery': row.delivery,
        'delivery_reason': row.delivery_reason,
        'purpose': SignedOriginal.PURPOSE_ARCHIVE if row.is_archive_copy else SignedOriginal.PURPOSE_ORIGINAL,
        'sent_at': israel_moment(row.sent_at),
        'paper_original_printed_at': israel_moment(row.paper_original_printed_at),
        'signed_at': israel_moment(row.signed_at),
    }


def delivery_lookup(keys: Iterable[tuple[str, object, str]]) -> dict[tuple[str, str], dict | None]:
    """
    {(kind, source_id): delivery_status or None} for (kind, source_id, number) keys, in one query.

    The row is found by its issuing row first, and by the fiscal number when
    none is keyed to it — the number is unique across every run kogo signs, so
    a store sale whose FormalDocument copy was the one signed still finds it.
    """
    wanted = [(kind, str(source_id), (number or '').strip()) for kind, source_id, number in keys]
    if not wanted:
        return {}
    ids_by_kind: dict[str, set[str]] = defaultdict(set)
    numbers: set[str] = set()
    for kind, source_id, number in wanted:
        ids_by_kind[kind].add(source_id)
        if number:
            numbers.add(number)

    match = Q(number__in=numbers) if numbers else Q(pk__in=[])
    for kind, ids in ids_by_kind.items():
        match |= Q(kind=kind, source_id__in=ids)

    by_source: dict[tuple[str, str], SignedOriginal] = {}
    by_number: dict[str, SignedOriginal] = {}
    for row in SignedOriginal.objects.filter(match).only(*_FIELDS):
        by_source.setdefault((row.kind, row.source_id), row)
        by_number[row.number] = row

    result: dict[tuple[str, str], dict | None] = {}
    for kind, source_id, number in wanted:
        row = by_source.get((kind, source_id)) or (by_number.get(number) if number else None)
        result[(kind, source_id)] = delivery_status(row) if row is not None else None
    return result
