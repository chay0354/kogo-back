"""A customer who holds an issued document is never deleted.

Deleting a record reaches the business's books through the foreign keys:

* ``Invoice.family`` and ``Payment.child`` / ``Payment.family`` are CASCADE — a
  family's delete took its issued receipts (the IR run) and every charge with it;
* ``InvoiceChild.child`` is CASCADE — a child's delete took the lines of the
  receipts that name it;
* ``FormalDocument.child`` / ``.business_customer`` and ``StoreInvoice.child``
  are SET_NULL — the document stayed, but printed with no customer.

Books and documents are kept for seven years after the tax year they belong to
(הוראה 25 להוראות מס הכנסה (ניהול פנקסי חשבונות)), and an issued document is
never changed. So the delete endpoints and the admin ask here first, and refuse
with the reason in words the office can act on — mark the child inactive
instead. A record with no document and no completed charge deletes as before.
"""
from __future__ import annotations

REFUSAL = (
    'לא ניתן למחוק: ללקוח יש מסמכים כספיים שהונפקו או חיובים שבוצעו. '
    'מסמכים נשמרים 7 שנים לפי הוראות ניהול פנקסי חשבונות, ומחיקה הייתה מוחקת אותם '
    'או משאירה אותם בלי שם הלקוח. אפשר להעביר את הילד לסטטוס "לא פעיל".'
)
# A charge that took money — refunded ones too: the refund is a credit note on it.
MONEY_STATUSES = ('completed', 'refunded')

BUSINESS_REFUSAL = (
    'לא ניתן למחוק לקוח עסקי שהונפקו לו מסמכים כספיים — המסמכים נשמרים 7 שנים '
    'לפי הוראות ניהול פנקסי חשבונות ויישארו בלי שם הלקוח.'
)


def child_holds_documents(child) -> bool:
    """True when deleting the child would take or strip an issued document or a completed charge."""
    from apps.customers.financial_models import InvoiceChild
    from apps.customers.models import Payment
    from apps.documents.models import FormalDocument
    from apps.store.models import StoreInvoice

    return (
        InvoiceChild.objects.filter(child=child).exists()
        or Payment.objects.filter(child=child, status__in=MONEY_STATUSES).exists()
        or FormalDocument.objects.filter(child=child).exclude(document_type='draft').exists()
        or StoreInvoice.objects.filter(child=child).exists()
    )


def family_holds_documents(family) -> bool:
    """True when deleting the family would take an issued receipt or a completed charge with it."""
    from apps.customers.financial_models import Invoice
    from apps.customers.models import Payment

    return (
        Invoice.objects.filter(family=family).exists()
        or Payment.objects.filter(family=family, status__in=MONEY_STATUSES).exists()
        or any(child_holds_documents(child) for child in family.children.all())
    )


def business_customer_holds_documents(customer) -> bool:
    from apps.documents.models import FormalDocument

    return FormalDocument.objects.filter(business_customer=customer).exclude(document_type='draft').exists()
