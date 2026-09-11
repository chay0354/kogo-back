"""Every document issued for a child, in one list, each with the route to its PDF.

תוספת ח׳ להוראות ניהול פנקסי חשבונות (school-type businesses) wants each student
linked to their documents. The office gets exactly that on the child's card:
every lesson receipt, store sale and manual document — credit notes included —
newest first. A receipt used to live only in the mail sent at charge time; this
is the way back to it.
"""
from __future__ import annotations

from django.db.models import Q
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from django.http import HttpResponse

from apps.core.permissions import IsManagerOrPartner
from apps.core.scoping import is_scoped_partner, partner_branch_ids
from apps.customers.financial_models import Invoice
from apps.documents.models import FormalDocument
from apps.store.models import StoreInvoice


def _day(value) -> str:
    if not value:
        return ''
    return (value.date() if hasattr(value, 'date') and callable(value.date) else value).isoformat()


def _late_issue(invoice) -> tuple[bool, str]:
    for log in invoice.activity_logs.all():
        if log.action == 'issued_late':
            return True, ((log.details or {}).get('money_received_at') or '')[:10]
    payment = invoice.payment if invoice.payment_id else None
    return False, _day(payment.payment_date) if payment is not None and payment.payment_date else ''


def _covered_payment_ids(invoice) -> list[str]:
    """Every charge a receipt covers — a family checkout issues one receipt for several."""
    for log in invoice.activity_logs.all():
        if log.action == 'checkout_lines':
            ids = (log.details or {}).get('payment_ids') or []
            if ids:
                return [str(value) for value in ids]
    return [str(invoice.payment_id)] if invoice.payment_id else []


def child_documents(child) -> list[dict]:
    rows: list[dict] = []

    receipts = (
        Invoice.objects
        .filter(Q(children__child=child) | Q(payment__child=child))
        .distinct()
        .select_related('payment')
        .prefetch_related('activity_logs')
    )
    for invoice in receipts:
        issued_late, paid_at = _late_issue(invoice)
        rows.append({
            'id': str(invoice.id),
            'kind': 'receipt',
            'document_number': invoice.invoice_number,
            'document_type': 'חשבונית מס/קבלה',
            'date': _day(invoice.invoice_date),
            'amount': str(invoice.amount),
            'status': invoice.status,
            'description': (invoice.payment.description if invoice.payment_id else '') or 'מנוי חוג',
            'payment_id': str(invoice.payment_id) if invoice.payment_id else None,
            'payment_ids': _covered_payment_ids(invoice),
            'issued_late': issued_late,
            'paid_at': paid_at,
            'download_url': f'/customers/invoices/{invoice.id}/pdf/',
        })

    for sale in StoreInvoice.objects.filter(child=child).prefetch_related('line_items__product'):
        products = [line.product.name for line in sale.line_items.all() if line.product_id]
        rows.append({
            'id': str(sale.id),
            'kind': 'store',
            'document_number': sale.invoice_number,
            'document_type': 'חשבונית עסקה' if sale.payment_method == 'monthly_billing' else 'חשבונית מס/קבלה',
            'date': _day(sale.issue_date),
            'amount': str(sale.total_amount),
            'status': sale.payment_status,
            'description': ', '.join(products) or 'רכישה בחנות',
            'payment_id': None,
            'payment_ids': [],
            'issued_late': False,
            'paid_at': _day(sale.issue_date) if sale.payment_status == 'completed' else '',
            'download_url': f'/store/invoices/{sale.id}/download/',
        })

    # Drafts are not issued documents, and a store sale issued through Tranzila
    # also has a FormalDocument copy — listed once, as the sale above.
    store_copies = StoreInvoice.objects.filter(child=child, formal_document__isnull=False).values_list(
        'formal_document_id', flat=True,
    )
    formal = FormalDocument.objects.filter(child=child).exclude(document_type='draft').exclude(id__in=store_copies)
    for doc in formal:
        rows.append({
            'id': str(doc.id),
            'kind': 'formal',
            'document_number': doc.document_number,
            'document_type': doc.get_document_type_display(),
            'date': _day(doc.document_date),
            'amount': str(doc.total_amount),
            'status': 'credit' if doc.document_type == 'credit_invoice' else 'issued',
            'description': doc.credit_reason or doc.description or '',
            'payment_id': None,
            'payment_ids': [],
            'issued_late': False,
            'paid_at': '',
            'download_url': f'/documents/documents/{doc.id}/pdf/',
        })

    rows.sort(key=lambda row: (row['date'], row['document_number']), reverse=True)
    return rows


class InvoicePdfView(APIView):
    """GET /api/v1/customers/invoices/{id}/pdf/ — a lesson receipt by its own id."""

    permission_classes = [IsAuthenticated, IsManagerOrPartner]

    def get(self, request, invoice_id):
        from apps.customers.subscription_invoice_pdf import generate_subscription_invoice_pdf

        invoice = Invoice.objects.select_related('family').filter(id=invoice_id).first()
        if invoice is None:
            return Response({'error': 'החשבונית לא נמצאה'}, status=status.HTTP_404_NOT_FOUND)
        if is_scoped_partner(request.user):
            branch_id = invoice.branch_id or (invoice.family.branch_id if invoice.family_id else None)
            allowed = {str(value) for value in partner_branch_ids(request.user)}
            if str(branch_id) not in allowed:
                return Response({'error': 'החשבונית לא נמצאה'}, status=status.HTTP_404_NOT_FOUND)

        response = HttpResponse(generate_subscription_invoice_pdf(invoice), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{invoice.invoice_number}.pdf"'
        return response
