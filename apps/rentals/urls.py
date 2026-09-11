from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.rentals.views import TenancyViewSet

router = DefaultRouter()
router.register(r'tenancies', TenancyViewSet, basename='tenancy')

urlpatterns = [
    path('', include(router.urls)),
]
