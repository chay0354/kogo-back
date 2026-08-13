from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsManager
from apps.core.registration_terms_service import get_registration_terms


class RegistrationTermsView(APIView):
    """
    GET — public HTML for registration widget.
    PUT — manager-only update.
    """

    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated(), IsManager()]

    def get(self, request):
        terms = get_registration_terms()
        return Response(
            {
                'content': terms.content,
                'updated_at': terms.updated_at.isoformat() if terms.updated_at else None,
            }
        )

    def put(self, request):
        content = request.data.get('content')
        if not isinstance(content, str) or not content.strip():
            return Response(
                {'detail': 'content is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        terms = get_registration_terms()
        terms.content = content.strip()
        terms.save(update_fields=['content', 'updated_at'])
        return Response(
            {
                'content': terms.content,
                'updated_at': terms.updated_at.isoformat(),
            }
        )
