import uuid
from datetime import date

from django.db.models import Q
from django.http import HttpResponse
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated

from apps.core.permissions import IsManagerOrPartner
from apps.core.scoping import is_scoped_partner, partner_branch_ids
from apps.signatures.models import Signature
from apps.signatures.pdf import generate_signature_pdf, signature_pdf_content_disposition
from apps.signatures.serializers import SignatureDetailSerializer, SignatureListSerializer


class SignaturePagination(PageNumberPagination):
    page_size = 50


def scope_signatures(qs, user):
    """
    Managers see every signature. A partner sees those whose branch — or whose
    family's branch — is one of theirs, and nothing when they have none.
    """
    if not is_scoped_partner(user):
        return qs
    ids = partner_branch_ids(user)
    if not ids:
        return qs.none()
    return qs.filter(Q(branch_id__in=ids) | Q(family__branch_id__in=ids))


def _uuid_param(params, name):
    raw = (params.get(name) or '').strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ValidationError({name: 'מזהה לא תקין'})


def _date_param(params, name):
    raw = (params.get(name) or '').strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValidationError({name: 'תאריך לא תקין (YYYY-MM-DD)'})


class SignatureViewSet(viewsets.ReadOnlyModelViewSet):
    """
    The signatures customers gave, for the office to see and download.

    GET /api/v1/signatures/?family=&child=&kind=&branch=&search=&date_from=&date_to=&page=
    GET /api/v1/signatures/{id}/
    GET /api/v1/signatures/{id}/pdf/
    """
    permission_classes = [IsAuthenticated, IsManagerOrPartner]
    pagination_class = SignaturePagination
    # Filtering is explicit below; the project-wide SearchFilter/OrderingFilter
    # would add a second, different meaning to ?search= and ?ordering=.
    filter_backends = []

    def get_queryset(self):
        qs = Signature.objects.select_related('family', 'branch').prefetch_related('children')
        qs = scope_signatures(qs, self.request.user)
        if self.action == 'list':
            # The image and the full text are for one signature at a time.
            qs = self._filter(qs.defer('signature_png', 'document_html'))
        return qs.order_by('-signed_at', '-created_at')

    def get_serializer_class(self):
        if self.action == 'list':
            return SignatureListSerializer
        return SignatureDetailSerializer

    def _filter(self, qs):
        params = self.request.query_params

        family_id = _uuid_param(params, 'family')
        if family_id:
            qs = qs.filter(family_id=family_id)
        child_id = _uuid_param(params, 'child')
        if child_id:
            qs = qs.filter(children__id=child_id)
        branch_id = _uuid_param(params, 'branch')
        if branch_id:
            qs = qs.filter(branch_id=branch_id)

        kind = (params.get('kind') or '').strip()
        if kind:
            qs = qs.filter(kind=kind)

        date_from = _date_param(params, 'date_from')
        if date_from:
            qs = qs.filter(signed_at__date__gte=date_from)
        date_to = _date_param(params, 'date_to')
        if date_to:
            qs = qs.filter(signed_at__date__lte=date_to)

        # Every word must match one of the signer's name, ID number or phone,
        # or a child's name. Matched through a subquery so the children join
        # never duplicates a row.
        for term in (params.get('search') or '').split():
            matching = Signature.objects.filter(
                Q(signer_name__icontains=term)
                | Q(signer_id_number__icontains=term)
                | Q(signer_phone__icontains=term)
                | Q(children__first_name__icontains=term)
                | Q(children__last_name__icontains=term)
            ).values('pk')
            qs = qs.filter(pk__in=matching)
        return qs

    @action(detail=True, methods=['get'], url_path='pdf')
    def pdf(self, request, pk=None):
        signature = self.get_object()
        response = HttpResponse(generate_signature_pdf(signature), content_type='application/pdf')
        response['Content-Disposition'] = signature_pdf_content_disposition(signature)
        return response
