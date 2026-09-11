from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.rentals.views import RentalContractViewSet, TenancyViewSet

router = DefaultRouter()
router.register(r'tenancies', TenancyViewSet, basename='tenancy')
router.register(r'contracts', RentalContractViewSet, basename='rental-contract')

urlpatterns = [
    path('', include(router.urls)),
]
