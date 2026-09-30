"""A business customer's card: who they are, every document issued to them, and where each went.

GET /api/v1/customers/business-customers/{id}/summary/ (BusinessCustomerViewSet.summary).

A merchant's or a studio tenant's documents — the ones issued by hand (TI, IRM,
RC, TX), the credit notes (CR) and the rent receipts (RT, apps/rental_billing)
— were reachable only by searching the invoices page, and the history from the
previous software only from the new-document wizard. The card puts them in one
place, the way the child's card does for a family (child_documents.py).

The answer, top to bottom:

  customer    the fields the card shows today (BusinessCustomerSerializer)
  consent     סעיף 18ב(ג): consent to computerized documents, given / withdrawn
  documents   issued FormalDocuments, newest first, each with delivery_status —
              how its signed original reached the customer (document_delivery.py)
  drafts      drafts apart: not documents yet, no number in any run, never signed
  legacy      the previous software's documents linked to the customer — managers
              only, as /legacy-import/documents/ is; null for anyone else
  tenancies   the studio rentals the customer holds, with their slots (read only)
  totals      what the documents add up to, by type
  balance     null — what is still open arrives with settlements (another stream)

Scoping: the customer itself comes through the viewset's get_object(), so a
partner reaches only the customers scope_business_customers gives them and any
other id is a 404. Inside the card a partner sees the documents whose branch is
one of theirs — the document's own branch, or the customer's when the document
names none — the rule the period report files documents by (period_report.py);
and the tenancies of their branches, as the tenancies screen shows them.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Prefetch

from apps.core.scoping import is_scoped_partner, partner_branch_ids, scope_branches
from apps.customers.document_delivery import delivery_lookup, israel_moment
from apps.documents.models import DOCUMENT_TYPE_CHOICES, FormalDocument, SignedOriginal

DRAFT = 'draft'
CREDIT = 'credit_invoice'
# What each total adds up. A חשבונית מס/קבלה both bills and receives; a
# transaction invoice (חשבונית עסקה) is a demand the tax invoice follows, so it
# counts only in its own line — adding it to "invoiced" would count the same
# money twice.
INVOICED_TYPES = ('tax_invoice', 'combined')
RECEIVED_TYPES = ('receipt', 'combined')
TYPE_LABELS = dict(DOCUMENT_TYPE_CHOICES)
# Newest agreement first, the running ones before those that ended.
TENANCY_STATUS_RANK = {'active': 0, 'signed': 1, 'sent': 2, 'draft': 3, 'ended': 4, 'cancelled': 5}
CENT = Decimal('0.01')


def _money(value) -> str:
    return str((value or Decimal('0')).quantize(CENT))


def _day(value) -> str:
    return value.isoformat() if value else ''


def _pdf_route(doc: FormalDocument) -> str:
    # The office copy (FormalDocumentViewSet.pdf): once originals are signed,
    # every print from the office says העתק; the original is the stored file.
    return f'/documents/documents/{doc.id}/pdf/'


def _scoped_documents(customer, user):
    qs = (
        FormalDocument.objects
        .filter(business_customer=customer)
        .select_related('branch')
        .order_by('-document_date', '-document_number')
    )
    if not is_scoped_partner(user):
        return qs
    from apps.documents.period_report import _partner_branch_q

    branch_ids = partner_branch_ids(user)
    if not branch_ids:
        return qs.none()
    return qs.filter(_partner_branch_q(branch_ids)).distinct()


def _document_row(doc: FormalDocument, delivery: dict | None) -> dict:
    from apps.documents.numbering import is_rental_number

    return {
        'id': str(doc.id),
        'document_number': doc.document_number,
        'document_type': doc.document_type,
        'document_type_label': doc.get_document_type_display(),
        'date': _day(doc.document_date),
        'total': _money(doc.total_amount),
        'is_credit': doc.document_type == CREDIT,
        # A rent receipt from the tenant's standing order (the RT run).
        'is_rental': is_rental_number(doc.document_number),
        'description': (doc.credit_reason if doc.document_type == CREDIT else '') or doc.description or '',
        'linked_document_number': doc.linked_document_number or '',
        'branch_name': doc.branch.name if doc.branch_id else '',
        'issued_at': israel_moment(doc.issued_at),
        'download_url': _pdf_route(doc),
        'delivery_status': delivery,
    }


def _draft_row(doc: FormalDocument) -> dict:
    return {
        'id': str(doc.id),
        'document_number': doc.document_number,
        'document_type': DRAFT,
        'document_type_label': TYPE_LABELS[DRAFT],
        # What it becomes once approved.
        'target_type': doc.draft_target_type or '',
        'target_type_label': TYPE_LABELS.get(doc.draft_target_type, doc.draft_target_type or ''),
        'date': _day(doc.document_date),
        'total': _money(doc.total_amount),
        'description': doc.description or '',
        'branch_name': doc.branch.name if doc.branch_id else '',
        'created_at': israel_moment(doc.created_at),
        'download_url': _pdf_route(doc),
    }


def _totals(docs: list[FormalDocument], drafts_count: int) -> dict:
    by_type: dict[str, dict] = {}
    invoiced = received = credited = Decimal('0')
    for doc in docs:
        amount = doc.total_amount or Decimal('0')
        line = by_type.setdefault(doc.document_type, {
            'document_type': doc.document_type,
            'label': doc.get_document_type_display(),
            'count': 0,
            'total': Decimal('0'),
        })
        line['count'] += 1
        line['total'] += amount
        if doc.document_type in INVOICED_TYPES:
            invoiced += amount
        if doc.document_type in RECEIVED_TYPES:
            received += amount
        if doc.document_type == CREDIT:
            credited += abs(amount)
    order = {value: index for index, (value, _label) in enumerate(DOCUMENT_TYPE_CHOICES)}
    return {
        'documents_count': len(docs),
        'drafts_count': drafts_count,
        'invoiced': _money(invoiced),
        'received': _money(received),
        'credited': _money(credited),
        'net_invoiced': _money(invoiced - credited),
        'by_type': [
            {**line, 'total': _money(line['total'])}
            for line in sorted(by_type.values(), key=lambda line: order.get(line['document_type'], len(order)))
        ],
    }


def _consent(customer) -> dict:
    return {
        'computerized_docs_consent_at': israel_moment(customer.computerized_docs_consent_at),
        'computerized_docs_consent_source': customer.computerized_docs_consent_source or '',
        'computerized_docs_consent_revoked_at': israel_moment(customer.computerized_docs_consent_revoked_at),
        'accepts_computerized_documents': customer.accepts_computerized_documents,
    }


def _legacy(customer) -> dict:
    from apps.legacy_import.models import LegacyDocument
    from apps.legacy_import.serializers import LegacyDocumentSerializer
    from apps.legacy_import.views import DOCUMENTS_LIMIT

    qs = (
        LegacyDocument.objects
        .filter(business_customer=customer)
        .select_related('business', 'business_category', 'branch')
        .order_by('-document_date', '-number')
    )
    count = qs.count()
    rows = list(qs[:DOCUMENTS_LIMIT])
    return {
        'count': count,
        'truncated': count > len(rows),
        'results': LegacyDocumentSerializer(rows, many=True).data,
    }


def _tenancies(customer, user) -> list[dict]:
    from apps.rentals.models import Tenancy
    from apps.rentals.serializers import TenancySlotSerializer
    from apps.scheduling.models import ScheduleEvent

    qs = scope_branches(
        Tenancy.objects.filter(tenant=customer).select_related('branch').prefetch_related(
            Prefetch(
                'slots',
                queryset=ScheduleEvent.objects.select_related('branch', 'studio')
                .order_by('event_date', 'start_time', 'created_at'),
            ),
        ),
        user,
        'branch',
    )
    tenancies = sorted(qs, key=lambda t: (TENANCY_STATUS_RANK.get(t.status, 9), -t.created_at.timestamp()))
    return [
        {
            'id': str(tenancy.id),
            'status': tenancy.status,
            'status_label': tenancy.get_status_display(),
            'branch_name': tenancy.branch.name if tenancy.branch_id else '',
            'monthly_amount': _money(tenancy.monthly_amount),
            'monthly_total': _money(tenancy.monthly_total),
            'billing_day': tenancy.billing_day,
            'start_date': _day(tenancy.start_date),
            'end_date': _day(tenancy.end_date),
            'slots': TenancySlotSerializer(tenancy.slots.all(), many=True).data,
        }
        for tenancy in tenancies
    ]


def business_customer_summary(customer, user, *, include_legacy: bool) -> dict:
    """The card's whole answer for `customer`, as `user` may see it (see the module docstring)."""
    from apps.customers.serializers import BusinessCustomerSerializer

    all_docs = list(_scoped_documents(customer, user))
    drafts = [doc for doc in all_docs if doc.document_type == DRAFT]
    issued = [doc for doc in all_docs if doc.document_type != DRAFT]

    found = delivery_lookup((SignedOriginal.KIND_FORMAL, doc.id, doc.document_number) for doc in issued)
    tenancies = _tenancies(customer, user)
    return {
        'customer': BusinessCustomerSerializer(customer).data,
        'consent': _consent(customer),
        'documents': [
            _document_row(doc, found.get((SignedOriginal.KIND_FORMAL, str(doc.id)))) for doc in issued
        ],
        'drafts': [_draft_row(doc) for doc in drafts],
        'legacy': _legacy(customer) if include_legacy else None,
        'is_tenant': bool(tenancies),
        'tenancies': tenancies,
        'totals': _totals(issued, len(drafts)),
        # What the customer still owes, and which invoice each receipt closed,
        # come with settlements (DocumentSettlement); until then the card says
        # nothing rather than a number it cannot back.
        'balance': None,
    }
