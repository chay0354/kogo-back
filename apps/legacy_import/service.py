"""Preview and commit of an export from the previous software — or from any other.

The file is read by its format (sources.py): the previous software's .xls
(parser.py), any software's table with the office's column mapping
(columns.py), or any software's מבנה אחיד files (uniform_reader.py). All three
produce the same rows, so from here on a source is only the `source_system`
every document is keyed under.

Preview reads the file, stores its normalised rows with a LegacyImport, and
says what a commit would do: the numbering per document type, the customers it
would create or update, the name changes, and where each location would go.
Nothing else is written.

Commit writes, in one transaction and from the stored rows only:

* the business customers — the ones the office issued documents to by hand
  (always) and the parents who paid for lessons by card (only when asked;
  most of them already exist in kogo as families). An existing card is found
  first, and its blanks filled; the newest name wins;
* every document, as a LegacyDocument, linked to its customer's card.

Both are idempotent. A document is keyed by (software, type, number); a customer by the
card their documents were linked to last time, then by ח"פ/ת"ז, email, and
phone with name. Committing the same file again finds everything it wrote and
changes nothing; committing a newer export updates what changed.

The writes are bulk: a full export is ~8,000 documents, and one query per row
would outlast a serverless request.
"""
from __future__ import annotations

import hashlib
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from apps.legacy_import import columns as table_columns
from apps.legacy_import import mapping as place_mapping
from apps.legacy_import import sources
from apps.legacy_import.models import SOURCE_TAZMAN, LegacyDocument, LegacyImport
from apps.legacy_import.parser import (
    REASON_LABELS,
    TYPE_LABELS,
    TYPE_ORDER,
    Customer,
    customers_from_rows,
    details_by_location,
    location_counts,
    name_changes,
    normalise_email,
    normalise_id,
    normalise_phone,
    plain_name,
    read_rows,
    type_table,
)
from apps.legacy_import.reader import ImportFileError

logger = logging.getLogger(__name__)

# Vercel refuses a request body over 4.5 MB before it reaches Django; the
# multipart envelope needs some of that. The owner's full export is 3.4 MB.
MAX_UPLOAD_BYTES = 4_300_000

NOTE_PREFIX = 'יובא מהתוכנה הקודמת:'


def note_prefix(source_system: str = SOURCE_TAZMAN) -> str:
    """The card's note line for one software. Each software's line replaces only its own."""
    if source_system == SOURCE_TAZMAN:
        return NOTE_PREFIX
    return f'יובא מתוכנה אחרת ({sources.source_label(source_system)}):'
PREVIEW_LIST_LIMIT = 500


class CommitInputError(ValueError):
    """The mapping sent with a commit points at something that does not exist."""


# --------------------------------------------------------------------------
# Matching a customer from the file to a card in kogo
# --------------------------------------------------------------------------

class CustomerIndex:
    """The business customers kogo has, findable the ways the file can name them."""

    def __init__(self, cards):
        self.by_id, self.by_email, self.by_phone_name = {}, {}, {}
        for card in cards:
            self.add(card)

    def add(self, card):
        for raw in (card.company_number, card.id_number):
            number = normalise_id(raw)
            if number:
                self.by_id.setdefault(number, card)
        email = normalise_email(card.email)
        if email:
            self.by_email.setdefault(email, card)
        phone = normalise_phone(card.phone)
        name = plain_name(card.full_name)
        if phone and name:
            self.by_phone_name.setdefault((phone, name), card)

    def match(self, customer: Customer):
        """(card, how) — by ח"פ/ת"ז, then email, then phone with any name the customer had."""
        for number in (customer.id_number, customer.dealer_number):
            if number and number in self.by_id:
                return self.by_id[number], 'id'
        if customer.email and customer.email in self.by_email:
            return self.by_email[customer.email], 'email'
        if customer.phone:
            for name in reversed(customer.names):
                card = self.by_phone_name.get((customer.phone, plain_name(name)))
                if card is not None:
                    return card, 'phone_name'
        return None, ''


MATCH_LABELS = {
    'linked': 'קושר בייבוא קודם',
    'id': 'לפי ת"ז / ח"פ',
    'email': 'לפי אימייל',
    'phone_name': 'לפי טלפון ושם',
}


def _load_cards() -> dict:
    """Every business customer, once: pk -> card. One object per row, so two
    customers of the file that land on the same card change the same object."""
    from apps.customers.models import BusinessCustomer

    return {card.pk: card for card in BusinessCustomer.objects.all()}


def _previously_linked(keys, cards: dict) -> dict:
    """customer key -> the card its documents were linked to by an earlier commit."""
    linked = {}
    rows = (
        LegacyDocument.objects
        .filter(customer_key__in=list(keys), business_customer__isnull=False)
        .order_by('customer_key', '-document_date', '-number')
        .values_list('customer_key', 'business_customer_id')
    )
    for key, card_id in rows:
        linked.setdefault(key, card_id)
    return {key: cards[card_id] for key, card_id in linked.items() if card_id in cards}


def _find_card(customer, linked, index):
    card = linked.get(customer.key)
    if card is not None:
        return card, 'linked'
    return index.match(customer)


class ParentIndex:
    """Phones and emails of kogo's parents — a card-paying customer who is already a family."""

    def __init__(self):
        from apps.customers.models import Parent

        self.phones, self.emails = set(), set()
        for phone, email in Parent.objects.values_list('phone', 'email'):
            if normalise_phone(phone):
                self.phones.add(normalise_phone(phone))
            if normalise_email(email):
                self.emails.add(normalise_email(email))

    def has(self, customer: Customer) -> bool:
        return bool(
            (customer.phone and customer.phone in self.phones)
            or (customer.email and customer.email in self.emails)
        )


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------

def _latest_order(customer: Customer):
    return (customer.latest['date'], customer.latest['number'])


def build_summary(rows: list, skipped: list, source_system: str = SOURCE_TAZMAN) -> dict:
    """Everything the preview shows, resolved against the database as it is now."""
    customers = customers_from_rows(rows)
    businesses, categories, branches = place_mapping.load_options()
    details = details_by_location(rows)
    locations = []
    for entry in location_counts(rows, customers):
        suggestion = place_mapping.suggest(
            entry['location'], details.get(entry['location'], []), businesses, categories, branches,
        )
        locations.append({**entry, **suggestion})

    cards = _load_cards()
    index = CustomerIndex(cards.values())
    linked = _previously_linked(customers.keys(), cards)
    parents = ParentIndex()

    business_rows, counts = [], Counter()
    for customer in sorted(customers.values(), key=_latest_order, reverse=True):
        card, how = _find_card(customer, linked, index)
        if customer.kind == 'business':
            if customer.deleted:
                counts['business_deleted'] += 1
            else:
                counts['business_update' if card else 'business_create'] += 1
            business_rows.append({
                'key': customer.key,
                'name': customer.full_name,
                'company_number': customer.company_number,
                'documents': customer.documents,
                'types': customer.types,
                'reasons': [REASON_LABELS[r] for r in customer.reasons],
                'latest': customer.latest,
                'deleted': customer.deleted,
                'action': 'skip' if customer.deleted else ('update' if card else 'create'),
                'match': {'id': str(card.pk), 'name': card.full_name, 'how': MATCH_LABELS[how]} if card else None,
            })
        else:
            counts['parents'] += 1
            if card is not None and how in ('linked', 'id'):
                counts['parents_matching_business_customer'] += 1
            if parents.has(customer):
                counts['parents_matching_family'] += 1

    keyed = sum(1 for row in rows if row['customer_key'])
    # Only this software's: another software's document with the same number is a different document.
    already = sum(
        LegacyDocument.objects.filter(
            source_system=source_system, doc_type=doc_type,
            number__in=[r['number'] for r in rows if r['doc_type'] == doc_type],
        ).count()
        for doc_type in TYPE_ORDER
    )
    dates = [row['date'] for row in rows]
    return {
        'documents': {
            'total': len(rows),
            'skipped': len(skipped),
            'skipped_rows': skipped[:50],
            'without_customer': len(rows) - keyed,
            'already_imported': already,
            'first_date': min(dates) if dates else None,
            'last_date': max(dates) if dates else None,
        },
        'types': type_table(rows),
        'customers': {
            'total': len(customers),
            'business': sum(1 for c in customers.values() if c.kind == 'business'),
            'business_create': counts['business_create'],
            'business_update': counts['business_update'],
            'business_deleted': counts['business_deleted'],
            'parents': counts['parents'],
            'parents_matching_family': counts['parents_matching_family'],
            'parents_matching_business_customer': counts['parents_matching_business_customer'],
            'business_list': business_rows[:PREVIEW_LIST_LIMIT],
        },
        'name_changes': name_changes(customers),
        'locations': locations,
        'options': place_mapping.options_payload(businesses, categories, branches),
    }


def _read_upload(upload) -> bytes:
    size = getattr(upload, 'size', None)
    if size is not None and size > MAX_UPLOAD_BYTES:
        raise ImportFileError(
            f'הקובץ גדול מדי ({size / 1_000_000:.1f}MB). הגבול הוא 4.3MB — '
            'ייצאו טווח תאריכים קצר יותר, והעלו כל חלק בנפרד.'
        )
    content = upload.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise ImportFileError('הקובץ גדול מדי. הגבול הוא 4.3MB.')
    return content


def describe_columns(upload) -> dict:
    """The mapping step of a table file: its columns, a few values of each, and the suggestion. Writes nothing."""
    from apps.legacy_import.tables import file_kind, read_table

    content = _read_upload(upload)
    sheet = read_table(content, getattr(upload, 'name', '') or '')
    if not sheet.rows:
        raise ImportFileError('בקובץ אין שורות מתחת לכותרות')
    return {**table_columns.describe(sheet), 'file_kind': file_kind(content)}


def _json_arg(value, label):
    """A multipart field that carries JSON (the mapping), or the dict itself."""
    import json

    if value in (None, ''):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ImportFileError(f'{label} אינו תקין') from exc


def _read_by_format(content, file_name, fmt, source_system, column_mapping, type_values, fixed_doc_type):
    """(rows, skipped, source details for the summary)."""
    if fmt == sources.FORMAT_TAZMAN:
        rows, skipped = read_rows(content)
        return rows, skipped, {}
    if fmt == sources.FORMAT_TABLE:
        from apps.legacy_import.tables import file_kind, read_table

        sheet = read_table(content, file_name)
        mapping = table_columns.clean_mapping(_json_arg(column_mapping, 'מיפוי העמודות'), sheet.headers)
        values = _json_arg(type_values, 'מיפוי סוגי המסמכים') or {}
        if not isinstance(values, dict):
            raise ImportFileError('מיפוי סוגי המסמכים אינו תקין')
        fixed = fixed_doc_type if fixed_doc_type in TYPE_LABELS else ''
        rows, skipped, unknown = table_columns.parse_table(
            sheet, mapping, source_system=source_system, type_values=values, fixed_doc_type=fixed,
        )
        return rows, skipped, {
            'file_kind': file_kind(content),
            'columns': {
                'headers': sheet.headers,
                'mapping': mapping,
                'type_values': {str(k): v for k, v in values.items() if v in TYPE_LABELS},
                'fixed_doc_type': fixed,
            },
            'unknown_types': unknown,
        }
    from apps.legacy_import.uniform_reader import read_uniform

    result = read_uniform(content, source_system=source_system)
    return result.rows, result.skipped, {'uniform': result.info}


def uniform_source_system(content: bytes) -> str:
    """The software a מבנה אחיד upload names in its INI.TXT (field 1007), normalised — or ''."""
    from apps.legacy_import.uniform_reader import find_files, read_ini

    found = find_files(content)
    for payload in found.ini.values():
        name = read_ini(payload).get('software', '')
        if name:
            return sources.normalise_source_system(name)
    return ''


def create_preview(upload, user, *, fmt: str = sources.FORMAT_TAZMAN, source_system: str = '',
                   column_mapping=None, type_values=None, fixed_doc_type: str = '') -> LegacyImport:
    """Read the uploaded file and keep what the preview needs. Raises ImportFileError."""
    if fmt not in sources.FORMATS:
        raise ImportFileError('סוג הקובץ שנבחר אינו מוכר')
    content = _read_upload(upload)
    file_name = (getattr(upload, 'name', '') or '')[:255]
    if fmt == sources.FORMAT_UNIFORM and not sources.normalise_source_system(source_system):
        source_system = uniform_source_system(content)
    source_system = sources.require_source_system(source_system, fmt=fmt)
    rows, skipped, details = _read_by_format(
        content, file_name, fmt, source_system, column_mapping, type_values, fixed_doc_type,
    )
    if not rows:
        reasons = Counter(s['reason'] for s in skipped).most_common(3)
        hint = ' (' + ' · '.join(f'{reason}: {count}' for reason, count in reasons) + ')' if reasons else ''
        raise ImportFileError('לא נמצאו בקובץ מסמכים לייבוא' + hint)
    summary = build_summary(rows, skipped, source_system)
    summary['source'] = {
        'format': fmt,
        'system': source_system,
        'label': sources.source_label(source_system),
        'file_kind': details.get('file_kind', 'xls' if fmt == sources.FORMAT_TAZMAN else 'txt'),
        'columns': details.get('columns'),
        'uniform': details.get('uniform'),
    }
    summary['unknown_types'] = details.get('unknown_types', [])
    fields = {
        'source_system': source_system,
        'file_name': file_name or 'export.xls',
        'row_count': len(rows),
        'rows': rows,
        'summary': summary,
        'uploaded_by': user if getattr(user, 'is_authenticated', False) else None,
    }
    sha256 = hashlib.sha256(content).hexdigest()
    # The same file uploaded again for another look (or another mapping) is the
    # same preview, with its summary read afresh: the rows are megabytes, and one copy is enough.
    legacy_import = LegacyImport.objects.filter(sha256=sha256, status=LegacyImport.STATUS_PREVIEW).first()
    if legacy_import is None:
        legacy_import = LegacyImport.objects.create(sha256=sha256, **fields)
    else:
        for name, value in fields.items():
            setattr(legacy_import, name, value)
        legacy_import.save(update_fields=list(fields))
    logger.info(
        'Legacy import %s previewed (%s, %s): %s documents, %s skipped, %s customers',
        legacy_import.pk, fmt, source_system, len(rows), len(skipped), summary['customers']['total'],
    )
    return legacy_import


# --------------------------------------------------------------------------
# Commit
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    business: object = None
    category: object = None
    branch: object = None


def resolve_mapping(raw) -> dict:
    """location -> Target. Refuses an id that does not exist, or a category of another business."""
    from apps.core.models import Branch, Business, BusinessCategory

    if raw in (None, ''):
        return {}
    if not isinstance(raw, dict):
        raise CommitInputError('המיפוי חייב להיות רשימת מיקומים')

    def ids(field):
        return {str(v[field]) for v in raw.values() if isinstance(v, dict) and v.get(field)}

    try:
        businesses = Business.objects.in_bulk(ids('business_id'))
        categories = BusinessCategory.objects.in_bulk(ids('category_id'))
        branches = Branch.objects.in_bulk(ids('branch_id'))
    except Exception as exc:  # noqa: BLE001 - a malformed UUID
        raise CommitInputError('במיפוי יש מזהה לא תקין') from exc
    businesses = {str(k): v for k, v in businesses.items()}
    categories = {str(k): v for k, v in categories.items()}
    branches = {str(k): v for k, v in branches.items()}

    resolved = {}
    for location, value in raw.items():
        if not isinstance(value, dict):
            continue
        business_id = str(value.get('business_id') or '')
        category_id = str(value.get('category_id') or '')
        branch_id = str(value.get('branch_id') or '')
        business = businesses.get(business_id) if business_id else None
        category = categories.get(category_id) if category_id else None
        branch = branches.get(branch_id) if branch_id else None
        if (business_id and business is None) or (category_id and category is None) or (branch_id and branch is None):
            raise CommitInputError(f'המיפוי של "{location}" מצביע על עסק, קטגוריה או סניף שאינם קיימים')
        if category is not None:
            if business is None:
                business = category.business
            elif category.business_id != business.pk:
                raise CommitInputError(f'במיפוי של "{location}" הקטגוריה אינה שייכת לעסק שנבחר')
        resolved[location] = Target(business, category, branch)
    return resolved


def split_name(first: str, last: str) -> tuple:
    """
    (first, last) for a card. kogo requires both: the documents wizard saves
    the card again on every document and refuses a blank last name. An
    organisation the old software kept in the first name alone is split the
    way the wizard splits a typed name — first word, then the rest.
    """
    first, last = (first or '').strip(), (last or '').strip()
    if not last:
        words = first.split()
        if len(words) > 1:
            first, last = words[0], ' '.join(words[1:])
    if not first and last:
        first, last = last, ''
    return first[:100], last[:100]


def import_note(customer: Customer, source_system: str = SOURCE_TAZMAN) -> str:
    latest = customer.latest
    when = date.fromisoformat(latest['date']).strftime('%d/%m/%Y')
    return (
        f"{note_prefix(source_system)} {customer.documents} מסמכים, "
        f"אחרון {latest['type_label']} {latest['number']} מ-{when}"
    )


def _notes_with(existing: str, line: str, source_system: str = SOURCE_TAZMAN) -> str:
    """The card's notes with this import's line — replacing the same software's earlier one, never repeated."""
    prefix = note_prefix(source_system)
    kept = [part for part in (existing or '').split('\n') if not part.startswith(prefix)]
    while kept and not kept[-1].strip():
        kept.pop()
    return '\n'.join(kept + [line]) if kept else line


def _apply_target(card, target: Target | None) -> None:
    """The customer's business, category and branch — from the location of their newest document."""
    if target is None:
        return
    if target.business is not None:
        card.business = target.business
        card.business_type = target.business.name[:100]
        card.business_category = target.category
        card.category = target.category.name[:100] if target.category else ''
        card.branch = target.branch
    elif target.branch is not None:
        card.branch = target.branch


CARD_FIELDS = (
    'first_name', 'last_name', 'email', 'phone', 'id_number', 'company_number', 'address',
    'business_id', 'business_category_id', 'branch_id', 'business_type', 'category', 'notes',
)


def _card_state(card) -> tuple:
    return tuple(getattr(card, field) for field in CARD_FIELDS)


def _update_card(card, customer: Customer, target: Target | None, source_system: str = SOURCE_TAZMAN) -> None:
    """Fill the card's blanks, give it the newest name, and file it where the newest document was."""
    card.first_name, card.last_name = split_name(customer.first_name, customer.last_name)
    if not card.email and customer.email:
        card.email = customer.email[:254]
    if not card.phone and customer.phone:
        card.phone = customer.phone[:20]
    if not card.address and customer.full_address:
        card.address = customer.full_address
    if not card.company_number and customer.company_number:
        card.company_number = customer.company_number[:20]
    if not card.id_number and customer.personal_id:
        card.id_number = customer.personal_id[:20]
    _apply_target(card, target)
    notes = card.notes or ''
    if not notes.strip() and customer.customer_notes:
        notes = customer.customer_notes
    card.notes = _notes_with(notes, import_note(customer, source_system), source_system)


def _new_card(customer: Customer, target: Target | None, source_system: str = SOURCE_TAZMAN):
    from apps.customers.models import BusinessCustomer

    card = BusinessCustomer(first_name='', last_name='', notes='')
    _update_card(card, customer, target, source_system)
    return card


def _optional_amount(value):
    return None if value in (None, '') else Decimal(str(value))


def _document_values(row: dict, card, target: Target | None) -> dict:
    return {
        'original_type': row['type_label'][:60],
        'document_date': date.fromisoformat(row['date']),
        'invoice_total': Decimal(row['invoice_total']),
        'receipt_total': Decimal(row['receipt_total']),
        'credit_total': Decimal(row['credit_total']),
        'withholding_amount': Decimal(row['withholding']),
        'total_before_withholding': Decimal(row['before_withholding']),
        'original_status': row['status'][:60],
        'payment_type': row['payment_type'][:60],
        'card_last_four': row['card_last_four'][:4],
        'location': row['location'][:300],
        'details': row['details'],
        'remark': row['remark'],
        'customer_key': row['customer_key'][:120],
        'customer_name': f"{row['first_name']} {row['last_name']}".strip()[:300],
        'customer_email': row['email'][:254],
        'customer_phone': row['phone'][:30],
        # Only other softwares' rows have these; the previous software's export says none of them.
        'original_number': (row.get('original_number') or '')[:40],
        'amount_before_vat': _optional_amount(row.get('amount_before_vat')),
        'vat_amount': _optional_amount(row.get('vat_amount')),
        'allocation_number': (row.get('allocation_number') or '')[:40],
        'linked_document': (row.get('linked_document') or '')[:60],
        'business_customer_id': card.pk if card is not None else None,
        'business_id': target.business.pk if target and target.business else None,
        'business_category_id': target.category.pk if target and target.category else None,
        'branch_id': target.branch.pk if target and target.branch else None,
    }


CARD_UPDATE_FIELDS = [
    'first_name', 'last_name', 'email', 'phone', 'id_number', 'company_number', 'address',
    'business', 'business_category', 'branch', 'business_type', 'category', 'notes', 'updated_at',
]
DOCUMENT_UPDATE_FIELDS = [
    'original_type', 'document_date', 'invoice_total', 'receipt_total', 'credit_total',
    'withholding_amount', 'total_before_withholding', 'original_status', 'payment_type',
    'card_last_four', 'location', 'details', 'remark', 'customer_key', 'customer_name',
    'customer_email', 'customer_phone', 'original_number', 'amount_before_vat', 'vat_amount',
    'allocation_number', 'linked_document', 'business_customer', 'business', 'business_category', 'branch',
]


def commit(import_id, mapping_payload, include_subscription_parents: bool, user,
           create_customers: bool = True) -> dict:
    """
    Write the import. Returns what was done; raises CommitInputError, LegacyImport.DoesNotExist.

    create_customers=False keeps the documents as history only: no card is
    opened or changed, and a document is linked to a card only when it is the
    same person (a card linked before, or the same ח"פ/ת"ז).
    """
    from apps.customers.models import BusinessCustomer

    with transaction.atomic():
        # Two clicks on "import" wait for each other instead of creating each card twice.
        legacy_import = LegacyImport.objects.select_for_update().get(pk=import_id)
        source_system = legacy_import.source_system or SOURCE_TAZMAN
        mapping = resolve_mapping(mapping_payload)
        rows = legacy_import.rows or []
        customers = customers_from_rows(rows)

        cards = _load_cards()
        index = CustomerIndex(cards.values())
        linked = _previously_linked(customers.keys(), cards)
        created, initial, counts, cards_by_key = [], {}, Counter(), {}

        ordered = sorted(customers.values(), key=_latest_order)
        # Deleted in the old software: the owner already decided this customer
        # has no future, so no card is created or changed for them.
        upserted = [
            c for c in ordered
            if create_customers and (c.kind == 'business' or include_subscription_parents) and not c.deleted
        ]
        upserted_keys = {c.key for c in upserted}
        # Oldest customer first: when two of the file's customers are one card
        # in kogo, the one with the newer documents is applied last and wins.
        for customer in upserted:
            card, _how = _find_card(customer, linked, index)
            target = mapping.get(customer.latest['location'])
            if card is None:
                card = _new_card(customer, target, source_system)
                created.append(card)
                index.add(card)
            else:
                # Compared against the card as it was before this commit, so a
                # card two customers share is "changed" only if it ends up different.
                initial.setdefault(card.pk, _card_state(card))
                _update_card(card, customer, target, source_system)
            cards_by_key[customer.key] = card

        # A customer who is not being made a card (a parent left out, or one
        # deleted in the old software) still has their documents shown on a
        # card that is the same person: one linked before, or the same ת"ז/ח"פ.
        # Not by email or phone — a community centre's coordinator who pays for
        # her own child with the office email is not the centre, and her
        # child's receipts do not belong in its history. After every card
        # exists, so the answer does not depend on the order customers came in.
        for customer in ordered:
            if customer.key in upserted_keys:
                continue
            if create_customers and customer.deleted and (customer.kind == 'business' or include_subscription_parents):
                counts['skipped_deleted'] += 1
            card, how = _find_card(customer, linked, index)
            if card is not None and how in ('linked', 'id'):
                cards_by_key[customer.key] = card
                if customer.kind == 'parent':
                    counts['parents_linked'] += 1
                elif customer.deleted:
                    counts['deleted_linked'] += 1
                else:
                    counts['history_linked'] += 1

        now = timezone.now()
        created_pks = {card.pk for card in created}
        updated = [
            cards[pk] for pk, state in initial.items()
            if pk not in created_pks and _card_state(cards[pk]) != state
        ]
        BusinessCustomer.objects.bulk_create(created, batch_size=500)
        for card in updated:
            card.updated_at = now
        BusinessCustomer.objects.bulk_update(updated, fields=CARD_UPDATE_FIELDS, batch_size=500)

        # Documents, keyed by (software, type, number): another software's
        # document with the same type and number is a different document.
        existing = {}
        for doc_type in TYPE_ORDER:
            numbers = [row['number'] for row in rows if row['doc_type'] == doc_type]
            if numbers:
                for doc in LegacyDocument.objects.filter(
                    source_system=source_system, doc_type=doc_type, number__in=numbers,
                ):
                    existing[(doc.doc_type, doc.number)] = doc
        new_docs, changed_docs, changed_fields = [], [], set()
        doc_counts = Counter()
        for row in rows:
            card = cards_by_key.get(row['customer_key'])
            values = _document_values(row, card, mapping.get(row['location']))
            doc = existing.get((row['doc_type'], row['number']))
            if doc is None:
                new_docs.append(LegacyDocument(
                    source_import=legacy_import, source_system=source_system,
                    doc_type=row['doc_type'], number=row['number'], **values,
                ))
                doc_counts['created'] += 1
            elif any(getattr(doc, field) != value for field, value in values.items()):
                for field, value in values.items():
                    if getattr(doc, field) != value:
                        setattr(doc, field, value)
                        changed_fields.add(field[:-3] if field.endswith('_id') else field)
                doc.source_import = legacy_import
                doc.updated_at = now
                changed_docs.append(doc)
                doc_counts['updated'] += 1
            else:
                doc_counts['unchanged'] += 1
            if card is not None:
                doc_counts['linked'] += 1
        LegacyDocument.objects.bulk_create(new_docs, batch_size=1000)
        if changed_docs:
            # Only the columns that moved: a newer export that links documents to
            # new cards rewrites one column of thousands of rows, not twenty-three.
            fields = [f for f in DOCUMENT_UPDATE_FIELDS if f in changed_fields] + ['source_import', 'updated_at']
            LegacyDocument.objects.bulk_update(changed_docs, fields=fields, batch_size=500)

        result = {
            'customers': {
                'created': len(created),
                'updated': len(updated),
                'unchanged': len(initial) - len(updated),
                'skipped_deleted': counts['skipped_deleted'],
                'deleted_linked_to_existing_cards': counts['deleted_linked'],
                'parents_included': bool(include_subscription_parents),
                'cards_opened_or_updated': bool(create_customers),
                'parents_linked_to_existing_cards': counts['parents_linked'],
                # create_customers=False: business customers whose documents found their card anyway.
                'linked_without_changing_cards': counts['history_linked'],
            },
            'documents': {
                'created': doc_counts['created'],
                'updated': doc_counts['updated'],
                'unchanged': doc_counts['unchanged'],
                'linked_to_customers': doc_counts['linked'],
                'total': len(rows),
            },
        }
        legacy_import.status = LegacyImport.STATUS_COMMITTED
        legacy_import.mapping = mapping_payload or {}
        legacy_import.include_subscription_parents = bool(include_subscription_parents)
        legacy_import.result = result
        legacy_import.committed_at = now
        legacy_import.committed_by = user if getattr(user, 'is_authenticated', False) else None
        legacy_import.save(update_fields=[
            'status', 'mapping', 'include_subscription_parents', 'result', 'committed_at', 'committed_by',
        ])
    logger.info('Legacy import %s committed: %s', legacy_import.pk, result)
    return result


# --------------------------------------------------------------------------
# Reading back
# --------------------------------------------------------------------------

def search_documents(queryset, term: str):
    """?q= — a name, email, phone, document number (as printed, too), ת"ז, or words from the details."""
    term = (term or '').strip()
    if not term:
        return queryset
    condition = (
        Q(customer_name__icontains=term) | Q(customer_email__icontains=term)
        | Q(details__icontains=term) | Q(location__icontains=term)
        | Q(original_number__iexact=term) | Q(allocation_number=term)
    )
    digits = ''.join(ch for ch in term if ch.isdigit())
    if digits and digits == term.replace('-', '').replace(' ', ''):
        condition |= Q(number=int(digits)) | Q(customer_phone__contains=normalise_phone(digits) or digits)
        if normalise_id(digits):
            condition |= Q(customer_key=normalise_id(digits))
    return queryset.filter(condition)


SERIES_ALL = 'all'


def series_summary(source_system: str = SOURCE_TAZMAN) -> list:
    """
    Per software and document type: the last number (and its date) of
    everything committed. It is the last number the files showed, not
    necessarily the software's last — see parser.type_table.

    By default only the previous software's (Tazman's): the numbering screen
    prefills "continue the previous software's run" from this, and another
    software's numbers must never become that. SERIES_ALL gives every software,
    the previous one first; each software's runs are its own.
    """
    if source_system == SERIES_ALL:
        systems = sorted(
            set(LegacyDocument.objects.values_list('source_system', flat=True).distinct()),
            key=lambda slug: (slug != SOURCE_TAZMAN, slug),
        )
    else:
        systems = [source_system]
    summary = []
    for source_system in systems:
        for doc_type in TYPE_ORDER:
            qs = LegacyDocument.objects.filter(source_system=source_system, doc_type=doc_type)
            agg = qs.aggregate(count=Count('id'), latest=Max('document_date'))
            if not agg['count']:
                continue
            last_doc = qs.order_by('-number').values('number', 'original_number', 'document_date').first()
            first_doc = qs.order_by('number').values('number', 'original_number', 'document_date').first()
            summary.append({
                'source_system': source_system,
                'source_label': sources.source_label(source_system),
                'doc_type': doc_type,
                'label': TYPE_LABELS[doc_type],
                'original_labels': sorted(set(qs.values_list('original_type', flat=True).distinct())),
                'count': agg['count'],
                'first_number': first_doc['number'],
                'first_printed': first_doc['original_number'],
                'first_date': first_doc['document_date'].isoformat(),
                'last_number': last_doc['number'],
                'last_printed': last_doc['original_number'],
                'last_date': last_doc['document_date'].isoformat(),
                'latest_date': agg['latest'].isoformat(),
            })
    return summary
