from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.signatures.views import SignatureViewSet

# SimpleRouter, not DefaultRouter: with an empty prefix the API root view
# would claim the same path as the list.
router = SimpleRouter()
router.register(r'', SignatureViewSet, basename='signature')

urlpatterns = [
    path('', include(router.urls)),
]
