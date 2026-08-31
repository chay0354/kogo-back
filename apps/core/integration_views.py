from django.conf import settings
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsManager


class IntegrationCredentialView(APIView):
    """
    Credentials a manager can set from inside the app.

    GET    /api/v1/core/auth/integration-credentials/  — which are set, and
           whether each comes from the environment or from a stored row
    POST   {key, value}                                — store one
    DELETE ?key=                                       — remove the stored copy

    A deployment's environment variables can only be changed by whoever holds
    the hosting account, which is not always the person who needs a feature
    working. The environment still wins wherever it carries a value, so adding
    the variable later retires the stored copy rather than competing with it.

    The value is write-only: it is never returned, and a caller learns only that
    something is present.
    """

    permission_classes = [IsAuthenticated, IsManager]

    # Only credentials the app knows what to do with, so this cannot become a
    # place to keep arbitrary secrets.
    ALLOWED_KEYS = {
        'SUPABASE_SERVICE_ROLE_KEY': 'אחסון תמונות המדריכים',
    }

    def _source(self, key, stored):
        if (getattr(settings, key, '') or '').strip():
            return 'environment'
        return 'stored' if key in stored else None

    def get(self, request):
        from apps.core.models import IntegrationCredential

        stored = set(
            IntegrationCredential.objects
            .filter(key__in=self.ALLOWED_KEYS)
            .values_list('key', flat=True)
        )
        return Response({
            'credentials': [
                {'key': key, 'label': label, 'source': self._source(key, stored)}
                for key, label in self.ALLOWED_KEYS.items()
            ]
        })

    def post(self, request):
        from apps.core.models import IntegrationCredential

        key = (request.data.get('key') or '').strip()
        value = (request.data.get('value') or '').strip()
        if key not in self.ALLOWED_KEYS:
            return Response({'error': 'הגדרה לא מוכרת'}, status=status.HTTP_400_BAD_REQUEST)
        if not value:
            return Response({'error': 'נדרש ערך'}, status=status.HTTP_400_BAD_REQUEST)

        IntegrationCredential.objects.update_or_create(
            key=key, defaults={'value': value, 'updated_by': request.user}
        )
        return Response({'key': key, 'source': 'stored'}, status=status.HTTP_201_CREATED)

    def delete(self, request):
        from apps.core.models import IntegrationCredential

        key = (request.query_params.get('key') or '').strip()
        if key not in self.ALLOWED_KEYS:
            return Response({'error': 'הגדרה לא מוכרת'}, status=status.HTTP_400_BAD_REQUEST)
        IntegrationCredential.objects.filter(key=key).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
