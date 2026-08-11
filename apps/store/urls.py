"""
Store URLs - API Route Configuration
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.store import views
from apps.store import widget_views

# Create router for ViewSets
router = DefaultRouter()
router.register(r'products', views.StoreProductViewSet, basename='store-product')
router.register(r'invoices', views.StoreInvoiceViewSet, basename='store-invoice')
router.register(r'sales', views.StoreSaleViewSet, basename='store-sale')

# URL patterns
urlpatterns = [
    # ViewSet routes
    path('', include(router.urls)),

    # B2C website integration (shared secret, not staff auth)
    path('integration/products/', widget_views.IntegrationProductsView.as_view(), name='store-integration-products'),
    path('integration/link/', widget_views.IntegrationLinkView.as_view(), name='store-integration-link'),
    path('integration/update/', widget_views.IntegrationProductUpdateView.as_view(), name='store-integration-update'),
    path('widget/stock-check/', widget_views.WidgetStoreStockCheckView.as_view(), name='store-widget-stock-check'),
    path('widget/order/', widget_views.WidgetStoreWebsiteOrderView.as_view(), name='store-widget-order'),
    path('widget/payment/initiate/', widget_views.WidgetStorePaymentInitiateView.as_view(), name='store-widget-payment-initiate'),
    
    # Payment endpoints
    path('payment/initiate/', views.initiate_payment, name='store-payment-initiate'),
    path('payment/charge-card/', views.charge_card, name='store-payment-charge-card'),
    path('payment/callback/', views.payment_callback, name='store-payment-callback'),
]

