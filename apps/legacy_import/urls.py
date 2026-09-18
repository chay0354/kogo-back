from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.legacy_import.views import LegacyImportViewSet

# SimpleRouter, not DefaultRouter: with an empty prefix the API root view
# would claim the same path as the list.
router = SimpleRouter()
router.register(r'', LegacyImportViewSet, basename='legacy-import')

urlpatterns = [
    path('', include(router.urls)),
]
