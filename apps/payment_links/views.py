"""CRM side: managers create links and read who paid. Partners never see this — it is money configuration."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum, Value
from django.db.models.functions import Coalesce
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import IsManager
from apps.payment_links.models import PaymentLink, PaymentLinkOption, PaymentLinkPayment, money
from apps.payment_links.serializers import (
    BusinessChargeCreateSerializer,
    PaymentLinkPaymentSerializer,
    PaymentLinkSerializer,
)


class PaymentLinkViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsManager]
    serializer_class = PaymentLinkSerializer
    pagination_class = None

    def get_queryset(self):
        completed = Q(payments__status=PaymentLinkPayment.STATUS_COMPLETED)
        return (
            PaymentLink.objects
            .select_related('business', 'business_category', 'branch')
            .prefetch_related('options')
            .annotate(
                paid_count=Count('payments', filter=completed, distinct=True),
                paid_total=Coalesce(Sum('payments__amount', filter=completed), Value(Decimal('0.00'))),
                review_count=Count('payments', filter=Q(payments__status=PaymentLinkPayment.STATUS_REVIEW), distinct=True),
            )
        )

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        # Links with payments are history; close them instead of deleting.
        link = self.get_object()
        if link.payments.exists():
            link.is_active = False
            link.save(update_fields=['is_active', 'updated_at'])
            return Response({'closed': True}, status=status.HTTP_200_OK)
        return super().destroy(request, *args, **kwargs)

    @action(detail=False, methods=['post'], url_path='business-charge')
    def business_charge(self, request):
        """Create one customer-specific, single-use Cogolive payment page."""
        serializer = BusinessChargeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        customer = data['business_customer']
        amount = money(data.pop('amount'))
        description = data.pop('description').strip()
        title = f'תשלום עבור {description}'[:120]
        with transaction.atomic():
            link = PaymentLink.objects.create(
                kind=PaymentLink.KIND_BUSINESS_CHARGE,
                title=title,
                description=description,
                created_by=request.user,
                **data,
            )
            PaymentLinkOption.objects.create(
                link=link, label=description[:120], amount=amount, sort_order=0,
            )
        return Response(PaymentLinkSerializer(link).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], url_path='payments')
    def payments(self, request, pk=None):
        link = self.get_object()
        qs = link.payments.select_related('link').order_by('-created_at')
        wanted = request.query_params.get('status')
        if wanted in dict(PaymentLinkPayment.STATUS_CHOICES):
            qs = qs.filter(status=wanted)
        return Response(PaymentLinkPaymentSerializer(qs[:500], many=True).data)

    @action(detail=True, methods=['post'], url_path='payments/(?P<payment_id>[0-9a-f-]+)/resolve')
    def resolve_review(self, request, pk=None, payment_id=None):
        """A person looked at a review row and decided: count it (completed) or not (failed)."""
        link = self.get_object()
        row = link.payments.filter(id=payment_id, status=PaymentLinkPayment.STATUS_REVIEW).first()
        if row is None:
            return Response({'error': 'לא נמצא תשלום לבדיקה'}, status=status.HTTP_404_NOT_FOUND)
        decision = request.data.get('decision')
        if decision == 'completed':
            row.status = PaymentLinkPayment.STATUS_COMPLETED
            if row.reported_amount is not None:
                row.amount = row.reported_amount
        elif decision == 'failed':
            row.status = PaymentLinkPayment.STATUS_FAILED
            row.failure_reason = (request.data.get('reason') or 'נדחה בבדיקה')[:500]
        else:
            return Response({'error': 'decision חייב להיות completed או failed'}, status=status.HTTP_400_BAD_REQUEST)
        row.save()
        if row.status == PaymentLinkPayment.STATUS_COMPLETED and link.kind == PaymentLink.KIND_BUSINESS_CHARGE:
            from apps.payment_links.business_charge import ensure_business_charge_document

            ensure_business_charge_document(row.id)
            row.refresh_from_db()
        return Response(PaymentLinkPaymentSerializer(row).data)

    @action(detail=True, methods=['post'], url_path='payments/(?P<payment_id>[0-9a-f-]+)/issue-document')
    def issue_document(self, request, pk=None, payment_id=None):
        """Retry only the document side of an already verified business charge."""
        link = self.get_object()
        row = link.payments.filter(id=payment_id).first()
        if row is None:
            return Response({'error': 'לא נמצא תשלום'}, status=status.HTTP_404_NOT_FOUND)
        if link.kind != PaymentLink.KIND_BUSINESS_CHARGE or row.status != PaymentLinkPayment.STATUS_COMPLETED:
            return Response({'error': 'אפשר להפיק מסמך רק לחיוב עסקי שאומת'}, status=status.HTTP_400_BAD_REQUEST)
        from apps.payment_links.business_charge import ensure_business_charge_document

        ensure_business_charge_document(row.id)
        row.refresh_from_db()
        code = status.HTTP_200_OK if row.formal_document_id else status.HTTP_409_CONFLICT
        return Response(PaymentLinkPaymentSerializer(row).data, status=code)
