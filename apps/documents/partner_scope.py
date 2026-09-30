"""
What a partner may see and do in the documents endpoints: their own branches only.

A partner (UserProfile.ROLE_PARTNER) reaches the documents list and detail, the
PDF, the allocation number, the payment reminder, creating a document, the check
and cash plans and the documents ledger (documents/tranzila). Until now every
one of them answered for the whole business. The rules here are the ones the
rest of the system already applies, so a partner sees the same documents in
every place:

* a FormalDocument belongs to the branch the period report files it under —
  its own branch, else its business customer's, else the child's family's,
  else the child's active enrollment (period_report._partner_branch_q, the
  same arms in the same order, so no document is in two partners' hands);
* a check or cash plan belongs to its own branch (the lesson's, else the
  family's, set when it was registered);
* a lesson receipt to its branch, else its family's; a store sale to its
  branch (register.py's rule for the same rows).

Managers are unchanged. A partner with no branch assigned sees nothing — the
fail-closed rule of apps/core/scoping. Another branch's document answers 404
(the queryset does not hold it); creating for another branch answers 403.
"""
from __future__ import annotations

from django.db.models import Q

from apps.core.scoping import (
    is_scoped_partner,
    partner_branch_ids,
    partner_visible_children_q,
    scope_business_customers,
)

NOT_YOUR_BRANCH = 'אין הרשאה לסניף הזה'
CHOOSE_BRANCH = 'יש לבחור סניף'
CHOOSE_LESSON = 'יש לבחור חוג באחד הסניפים שלך'
CHILD_NOT_FOUND = 'הילד לא נמצא'
CUSTOMER_NOT_FOUND = 'הלקוח לא נמצא'
LINKED_NOT_YOURS = 'המסמך המקושר שייך לסניף אחר'


def partner_branches(user):
    """None for a reader of every branch; the partner's branch ids (maybe none) otherwise."""
    if not is_scoped_partner(user):
        return None
    return list(partner_branch_ids(user))


def document_branch_q(branch_ids) -> Q:
    """FormalDocuments filed under one of `branch_ids` — the period report's rule."""
    from apps.documents.period_report import _partner_branch_q

    return _partner_branch_q(branch_ids)


def scope_documents(queryset, user):
    ids = partner_branches(user)
    if ids is None:
        return queryset
    if not ids:
        return queryset.none()
    # distinct(): the enrollment arm joins a to-many relation.
    return queryset.filter(document_branch_q(ids)).distinct()


def scope_plans(queryset, user):
    """Check plans and cash plans: by the plan's own branch."""
    ids = partner_branches(user)
    if ids is None:
        return queryset
    if not ids:
        return queryset.none()
    return queryset.filter(branch_id__in=ids)


def lesson_receipt_branch_q(branch_ids) -> Q:
    """A lesson receipt's branch, else its family's (register.lesson_documents)."""
    return Q(branch_id__in=branch_ids) | Q(branch__isnull=True, family__branch_id__in=branch_ids)


def _visible_child(child_id, ids):
    from apps.customers.models import Child

    return (
        Child.objects.filter(pk=child_id).filter(partner_visible_children_q(ids))
        .select_related('family').distinct().first()
    )


def _linked_elsewhere(number: str, user, ids) -> bool:
    """True when kogo holds a document by this number and it is another branch's."""
    from apps.customers.financial_models import Invoice
    from apps.documents.models import FormalDocument
    from apps.store.models import StoreInvoice

    number = (number or '').strip()
    if not number:
        return False
    formal = FormalDocument.objects.filter(document_number=number)
    if formal.exists():
        return not scope_documents(formal, user).exists()
    lesson = Invoice.objects.filter(invoice_number=number)
    if lesson.exists():
        return not lesson.filter(lesson_receipt_branch_q(ids)).exists()
    sale = StoreInvoice.objects.filter(invoice_number=number)
    if sale.exists():
        return not sale.filter(branch_id__in=ids).exists()
    # A number kogo never issued (the previous software's): nothing to hide.
    return False


def document_create_refusal(user, data: dict):
    """
    None when `user` may create this document; else (HTTP status, message).

    For a partner the customer must be one they can see, the document must be
    filed under one of their branches, and a document it names (a credit
    note's, a receipt's invoice) must not be another branch's. A document that
    would be filed under no branch takes the partner's branch when they have
    one — or they are asked to choose — so they can find what they issued.
    Sets data['branch_id'] in that case.
    """
    from apps.customers.models import BusinessCustomer

    ids = partner_branches(user)
    if ids is None:
        return None
    if not ids:
        return 403, NOT_YOUR_BRANCH
    allowed = {str(value) for value in ids}

    child = customer = None
    if data.get('child_id'):
        child = _visible_child(data['child_id'], ids)
        if child is None:
            return 404, CHILD_NOT_FOUND
    if data.get('business_customer_id'):
        customer = scope_business_customers(
            BusinessCustomer.objects.filter(pk=data['business_customer_id']), user,
        ).first()
        if customer is None:
            return 404, CUSTOMER_NOT_FOUND

    # Where the document will be filed: the branch sent, else the family's
    # (service._branch_for), else the business customer's (resolve_branch).
    branch = data.get('branch_id')
    if not branch and child is not None and child.family_id:
        branch = child.family.branch_id
    if not branch and customer is not None:
        branch = customer.branch_id
    if branch:
        if str(branch) not in allowed:
            return 403, NOT_YOUR_BRANCH
    elif len(ids) == 1:
        data['branch_id'] = ids[0]
    else:
        return 400, CHOOSE_BRANCH

    for section in ('receipt_details', 'credit_invoice_details'):
        linked = (data.get(section) or {}).get('linked_invoice_id')
        if linked and _linked_elsewhere(linked, user, ids):
            return 403, LINKED_NOT_YOURS
    # The invoices a receipt pays (settlement.py) must be the partner's own too.
    invoice_ids = [row.get('invoice_id') for row in data.get('settlements') or [] if row.get('invoice_id')]
    if invoice_ids:
        from apps.documents.models import FormalDocument

        found = FormalDocument.objects.filter(pk__in=invoice_ids)
        if scope_documents(found, user).count() != found.count():
            return 403, LINKED_NOT_YOURS
    return None


def plan_create_refusal(user, child_id, lesson_id):
    """
    None when `user` may register a check or cash plan for this child; else (HTTP status, message).

    The plan is filed under its lesson's branch, else the family's
    (check_plans/cash_plans): for a partner that has to be one of theirs.
    """
    from apps.courses.models import Lesson

    ids = partner_branches(user)
    if ids is None:
        return None
    if not ids:
        return 403, NOT_YOUR_BRANCH
    allowed = {str(value) for value in ids}

    child = _visible_child(child_id, ids)
    if child is None:
        return 404, CHILD_NOT_FOUND
    branch = None
    if lesson_id:
        lesson = Lesson.objects.select_related('course').filter(pk=lesson_id).first()
        if lesson is not None and lesson.course_id:
            branch = lesson.course.branch_id
    if branch is None and child.family_id:
        branch = child.family.branch_id
    if branch is None:
        return 400, CHOOSE_LESSON
    if str(branch) not in allowed:
        return 403, NOT_YOUR_BRANCH
    return None
