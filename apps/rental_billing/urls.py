from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.rental_billing.views import (
    BillingStatusView,
    PublicCardView,
    StandingOrderViewSet,
    TenantChargeViewSet,
    cron_charge,
)

router = DefaultRouter()
router.register(r'standing-orders', StandingOrderViewSet, basename='rental-standing-order')
router.register(r'charges', TenantChargeViewSet, basename='rental-charge')

urlpatterns = [
    path('status/', BillingStatusView.as_view(), name='rental-billing-status'),
    path('cron/charge/', cron_charge, name='rental-billing-cron-charge'),
    path('card/<str:token>/', PublicCardView.as_view(), name='rental-billing-card'),
    path('', include(router.urls)),
]
