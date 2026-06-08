from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.models import UserProfile
from apps.core.partner_serializers import PartnerSerializer
from apps.core.permissions import IsManager


User = get_user_model()


class PartnerViewSet(viewsets.ModelViewSet):
    """
    Manager-only CRUD for partner users (role=partner).
    """

    serializer_class = PartnerSerializer
    permission_classes = [IsAuthenticated, IsManager]
    http_method_names = ['get', 'post', 'patch', 'put', 'delete', 'head', 'options']

    def get_queryset(self):
        qs = (
            User.objects.filter(profile__role=UserProfile.ROLE_PARTNER)
            .select_related('profile')
            .prefetch_related('profile__assigned_branches')
            .order_by('first_name', 'last_name', 'email')
        )

        search = self.request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(email__icontains=search)
            )

        branch_id = self.request.query_params.get('branch')
        if branch_id and branch_id != 'all':
            qs = qs.filter(profile__assigned_branches__id=branch_id).distinct()

        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return Response({
            'partners': serializer.data,
            'summary': {'total_partners': queryset.count()},
        })

    def destroy(self, request, *args, **kwargs):
        user = self.get_object()
        user.is_active = False
        user.save(update_fields=['is_active'])
        return Response(status=204)
