from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.rentals.public_views import SigningPageView, SigningPdfView
from apps.rentals.views import RentalContractViewSet, TenancyViewSet

router = DefaultRouter()
router.register(r'tenancies', TenancyViewSet, basename='tenancy')
router.register(r'contracts', RentalContractViewSet, basename='rental-contract')

urlpatterns = [
    # The tenant's signing page: no login, the token is the key (public_views.py).
    path('sign/<str:token>/', SigningPageView.as_view(), name='rental-sign'),
    path('sign/<str:token>/pdf/', SigningPdfView.as_view(), name='rental-sign-pdf'),
    path('', include(router.urls)),
]
