"""The import from the previous software, and the history it leaves behind.

    POST /api/v1/legacy-import/preview/            multipart `file` -> the preview
    GET  /api/v1/legacy-import/                    recent imports (no rows)
    GET  /api/v1/legacy-import/{id}/               one import's preview/result
    POST /api/v1/legacy-import/{id}/commit/        {mapping, include_subscription_parents}
    GET  /api/v1/legacy-import/documents/?business_customer=&q=
    GET  /api/v1/legacy-import/series/             last number per original type

Managers only, like the register and the period report: it writes customer
cards in bulk and shows every document the business issued.
"""
import logging
import uuid

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import IsManager
from apps.legacy_import import service
from apps.legacy_import.models import LegacyDocument, LegacyImport
from apps.legacy_import.reader import ImportFileError
from apps.legacy_import.serializers import (
    LegacyDocumentSerializer,
    LegacyImportListSerializer,
    LegacyImportSerializer,
)

logger = logging.getLogger(__name__)

# A customer's history is shown whole; a search is a lookup, not a report.
DOCUMENTS_LIMIT = 500


class LegacyImportViewSet(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated, IsManager]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    serializer_class = LegacyImportSerializer
    # Filtering is explicit below; the project-wide SearchFilter would give ?search= a second meaning.
    filter_backends = []

    def get_queryset(self):
        # The rows are megabytes and only the commit reads them.
        return LegacyImport.objects.defer('rows').select_related('uploaded_by')

    def list(self, request):
        imports = self.get_queryset().defer('rows', 'summary')[:10]
        return Response(LegacyImportListSerializer(imports, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(LegacyImportSerializer(self.get_object()).data)

    @action(detail=False, methods=['post'])
    def preview(self, request):
        upload = request.FILES.get('file')
        if upload is None:
            return Response({'error': 'לא נבחר קובץ'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            legacy_import = service.create_preview(upload, request.user)
        except ImportFileError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(LegacyImportSerializer(legacy_import).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def commit(self, request, pk=None):
        legacy_import = self.get_object()
        include_parents = request.data.get('include_subscription_parents', False)
        if not isinstance(include_parents, bool):
            include_parents = str(include_parents).lower() in ('1', 'true', 'yes')
        try:
            result = service.commit(
                legacy_import.pk, request.data.get('mapping') or {}, include_parents, request.user,
            )
        except service.CommitInputError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @action(detail=False, methods=['get'])
    def documents(self, request):
        qs = LegacyDocument.objects.select_related('business', 'business_category', 'branch')
        customer = (request.query_params.get('business_customer') or '').strip()
        term = (request.query_params.get('q') or '').strip()
        if not customer and not term:
            raise ValidationError({'error': 'יש לבחור לקוח או להקליד חיפוש'})
        if customer:
            try:
                qs = qs.filter(business_customer_id=uuid.UUID(customer))
            except ValueError:
                raise ValidationError({'business_customer': 'מזהה לא תקין'})
        qs = service.search_documents(qs, term).order_by('-document_date', '-number')
        count = qs.count()
        rows = list(qs[:DOCUMENTS_LIMIT])
        return Response({
            'count': count,
            'truncated': count > len(rows),
            'results': LegacyDocumentSerializer(rows, many=True).data,
        })

    @action(detail=False, methods=['get'])
    def series(self, request):
        return Response({'series': service.series_summary()})
