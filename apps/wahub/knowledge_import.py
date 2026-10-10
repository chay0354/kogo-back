"""
The one-time import of the old bot's knowledge (knowledge_seed.py) into KnowledgeItem
rows, plus the two tags and two ready-made replies the panel held.

Idempotent by `source_note`: a record that is already there is skipped, so the
owner's edits after the import survive a second run. With the docs folder at
hand, every phrasing marked "מילה במילה" is checked against the texts it was
copied from, and a mismatch is reported (not imported differently).
"""
from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings

from apps.wahub import knowledge, knowledge_seed, state
from apps.wahub.models import KIND_PHRASING, SCOPE_BRANCH, SCOPE_BUSINESS, SCOPE_CITY, KnowledgeItem, QuickReply, Tag

# kogo-system/docs/bot-knowledge — beside the main checkout (kogo-back/) or two levels up from a worktree (worktrees/<name>/).
DOCS_DIR_CANDIDATES = tuple(
    Path(settings.BASE_DIR).resolve().parents[levels] / 'docs' / 'bot-knowledge' for levels in (0, 1)
)
DEFAULT_DOCS_DIR = next((folder for folder in DOCS_DIR_CANDIDATES if folder.is_dir()), DOCS_DIR_CANDIDATES[0])


def _resolve_scope(level: str, label: str):
    """The Kogo row behind a scope label from the texts; the label alone when there is none."""
    if level == SCOPE_BRANCH:
        from apps.core.models import Branch

        for word in re.split(r'\s*/\s*', label):
            found = Branch.objects.filter(name__icontains=word.strip(), is_active=True).values_list('id', 'name').first()
            if found:
                return str(found[0]), found[1]
    if level == SCOPE_CITY:
        from apps.core.models import City

        found = City.objects.filter(name__iexact=label).values_list('id', 'name').first()
        if found:
            return str(found[0]), found[1]
    return '', label


def _normalize(text: str) -> str:
    text = text.replace(' ', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def _docs_text(docs_dir) -> str:
    folder = Path(docs_dir) if docs_dir else DEFAULT_DOCS_DIR
    if not folder.is_dir():
        return ''
    parts = []
    for path in sorted(folder.glob('0[1-3]-*.md')):
        try:
            parts.append(path.read_text(encoding='utf-8'))
        except OSError:
            continue
    return _normalize('\n'.join(parts))


_PLACEHOLDER = re.compile(r'\{[^}]+\}')


def _verbatim_in_docs(record: dict, docs: str) -> bool:
    """
    The phrasing, word for word, somewhere in the texts. A {משתנה} the texts
    wrote as "[שם הלקוח]" or "[האזור שנמסר בשלב 3]" stands for a short stretch
    of anything; the words around it must match exactly.
    """
    body = record['body']
    for variable, value in knowledge_seed.VARIABLES.items():
        body = body.replace(variable, value)
    parts = [re.escape(part) for part in _PLACEHOLDER.split(_normalize(body))]
    return re.search('.{0,80}?'.join(parts), docs) is not None


def run(*, today=None, docs_dir=None, user=None, dry_run: bool = False) -> dict:
    today = today or state.now_israel_date()
    docs = _docs_text(docs_dir)
    result = {
        'created': {}, 'skipped': 0, 'inactive': 0, 'tags': 0, 'quick_replies': 0,
        'verbatim_checked': 0, 'verbatim_mismatch': [], 'docs_found': bool(docs), 'dropped': knowledge_seed.DROPPED,
    }
    existing = set(KnowledgeItem.objects.filter(source_note__startswith=knowledge_seed.IMPORT_TAG).values_list('source_note', flat=True))

    for record in knowledge_seed.all_items(today):
        record = dict(record)
        data = dict(record.pop('data', {}) or {})
        if record['kind'] == KIND_PHRASING and data.get('verbatim') and docs:
            result['verbatim_checked'] += 1
            if not _verbatim_in_docs(record, docs):
                result['verbatim_mismatch'].append(record['key'])
        if record['source_note'] in existing:
            result['skipped'] += 1
            continue
        if record['scope_level'] != SCOPE_BUSINESS:
            record['scope_id'], record['scope_label'] = _resolve_scope(record['scope_level'], record['scope_label'])
        if not record['is_active']:
            result['inactive'] += 1
        result['created'][record['kind']] = result['created'].get(record['kind'], 0) + 1
        if dry_run:
            continue
        knowledge.create_item({**record, 'data': data}, user, note='ייבוא מהבוט הישן')
        existing.add(record['source_note'])

    if not dry_run:
        for name, color in knowledge_seed.TAGS:
            _, created = Tag.objects.get_or_create(name=name, defaults={'color': color})
            result['tags'] += int(created)
        for title, text in knowledge_seed.QUICK_REPLIES:
            _, created = QuickReply.objects.get_or_create(title=title, defaults={'text': text})
            result['quick_replies'] += int(created)
    result['created_total'] = sum(result['created'].values())
    return result
