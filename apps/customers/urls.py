from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views
from .widget_views import WidgetLookupView, WidgetRegisterView

router = DefaultRouter()
router.register(r'families', views.FamilyViewSet, basename='family')
router.register(r'parents', views.ParentViewSet, basename='parent')
router.register(r'children', views.ChildViewSet, basename='child')
# Store endpoints moved to apps.store.urls
router.register(r'discounts', views.DiscountViewSet, basename='discount')
router.register(r'payments', views.PaymentViewSet, basename='payment')
router.register(r'recurring-payments', views.RecurringPaymentViewSet, basename='recurring-payment')

urlpatterns = [
    path('', include(router.urls)),
    path('widget/lookup/', WidgetLookupView.as_view(), name='widget-lookup'),
    path('widget/register/', WidgetRegisterView.as_view(), name='widget-register'),
]

