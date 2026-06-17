import logging
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q
from django.core.mail import send_mail
from django.conf import settings

from apps.core.permissions import IsManagerOrPartner
from apps.documents.models import FormalDocument
from apps.documents.serializers import (
    FormalDocumentSerializer,
    FormalDocumentListSerializer,
    CreateDocumentSerializer,
)
from apps.documents import service

logger = logging.getLogger(__name__)


class FormalDocumentViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsManagerOrPartner]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['document_number', 'description']
    ordering_fields = ['document_date', 'created_at', 'total_amount']
    ordering = ['-created_at']

    def get_queryset(self):
        qs = FormalDocument.objects.select_related('child', 'business_customer', 'branch')

        doc_type = self.request.query_params.get('document_type')
        if doc_type:
            qs = qs.filter(document_type=doc_type)

        child_id = self.request.query_params.get('child_id')
        if child_id:
            qs = qs.filter(child_id=child_id)

        business_customer_id = self.request.query_params.get('business_customer_id')
        if business_customer_id:
            qs = qs.filter(business_customer_id=business_customer_id)

        # Exclude credit invoices from the "open invoices" list when requested
        exclude_credits = self.request.query_params.get('exclude_credits')
        if exclude_credits:
            qs = qs.exclude(document_type='credit_invoice')

        return qs

    def get_serializer_class(self):
        if self.action == 'list':
            return FormalDocumentListSerializer
        return FormalDocumentSerializer

    @action(detail=False, methods=['post'], url_path='create-document')
    def create_document(self, request):
        """
        Unified endpoint for all document types.
        POST /api/v1/documents/documents/create-document/
        """
        serializer = CreateDocumentSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        doc_type = data['document_type']

        try:
            if doc_type in ('tax_invoice', 'transaction_invoice'):
                doc = service.create_invoice(data, doc_type)
            elif doc_type == 'combined':
                doc = service.create_combined(data)
            elif doc_type == 'receipt':
                doc = service.create_receipt(data)
            elif doc_type == 'credit_invoice':
                doc = service.create_credit_invoice(data)
            else:
                return Response({'error': f'סוג מסמך לא נתמך: {doc_type}'}, status=status.HTTP_400_BAD_REQUEST)

            out = FormalDocumentSerializer(doc)
            return Response(out.data, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"Document creation failed: {e}", exc_info=True)
            return Response({'error': f'שגיאה ביצירת המסמך: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'], url_path='send-reminder')
    def send_reminder(self, request, pk=None):
        """
        POST /api/v1/documents/documents/{id}/send-reminder/
        Sends a payment reminder email to the customer.
        """
        doc = self.get_object()

        # Resolve recipient email
        email = None
        customer_name = ''
        if doc.client_type == 'business' and doc.business_customer:
            email = doc.business_customer.email or None
            customer_name = doc.business_customer.full_name
        elif doc.client_type == 'existing' and doc.child:
            family = getattr(doc.child, 'family', None)
            if family:
                email = family.email or None
                customer_name = doc.child.full_name

        if not email:
            return Response({'error': 'no_email'}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        due_date_str = doc.due_date.strftime('%d/%m/%Y') if doc.due_date else 'לא הוגדר'
        subject = f'תזכורת תשלום — מסמך {doc.document_number}'
        body = (
            f'שלום {customer_name},\n\n'
            f'זוהי תזכורת לתשלום עבור {doc.document_type_display if hasattr(doc, "document_type_display") else "מסמך"} '
            f'מספר {doc.document_number}.\n\n'
            f'סכום לתשלום: ₪{doc.total_amount}\n'
            f'תאריך פירעון: {due_date_str}\n\n'
            f'בברכה,\nצוות קוגומלו'
        )

        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@kogomalo.com')
        try:
            send_mail(subject, body, from_email, [email], fail_silently=False)
        except Exception as e:
            logger.error(f"Reminder email failed for doc {pk}: {e}", exc_info=True)
            return Response({'error': 'send_failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({'sent': True})
