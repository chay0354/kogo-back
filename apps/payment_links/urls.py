from django.urls import path, include
from rest_framework.routers import DefaultRouter

from apps.payment_links import public_views, views

router = DefaultRouter()
router.register(r'links', views.PaymentLinkViewSet, basename='payment-link')

urlpatterns = [
    path('', include(router.urls)),
    path('public/callback/', public_views.payment_link_callback, name='payment-link-callback'),
    path('public/payments/<uuid:payment_id>/status/', public_views.PublicPaymentStatusView.as_view(), name='payment-link-status'),
    path('public/<str:slug>/', public_views.PublicPaymentLinkView.as_view(), name='payment-link-public'),
    path('public/<str:slug>/start/', public_views.PublicPaymentStartView.as_view(), name='payment-link-start'),
]
