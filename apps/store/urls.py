"""
Store URLs - API Route Configuration
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.store import views

# Create router for ViewSets
router = DefaultRouter()
router.register(r'products', views.StoreProductViewSet, basename='store-product')
router.register(r'invoices', views.StoreInvoiceViewSet, basename='store-invoice')
router.register(r'sales', views.StoreSaleViewSet, basename='store-sale')

# URL patterns
urlpatterns = [
    # ViewSet routes
    path('', include(router.urls)),
    
    # Payment endpoints
    path('payment/initiate/', views.initiate_payment, name='store-payment-initiate'),
    path('payment/charge-card/', views.charge_card, name='store-payment-charge-card'),
    path('payment/callback/', views.payment_callback, name='store-payment-callback'),
]

