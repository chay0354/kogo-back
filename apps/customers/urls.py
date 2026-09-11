from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views
from .card_update_views import CardUpdateChargeView, CardUpdatePreviewView
from .child_documents import InvoicePdfView
from .card_link_views import (
    CardLinkActionView,
    CardLinkChargeView,
    CardLinkListCreateView,
    CardLinkOptionsView,
    CardLinkPreviewView,
)
from .widget_views import (
    WidgetLookupView,
    WidgetRegisterView,
    WidgetTrialRegisterView,
    WidgetChargeView,
    WidgetPaymentStatusView,
    WidgetCitiesView,
    WidgetBranchesView,
    WidgetCoursesView,
    WidgetCourseTypesView,
    WidgetLessonOccurrencesView,
    WidgetTermsView,
)
from .views import cron_recurring_billing, cron_recurring_billing_status

router = DefaultRouter()
router.register(r'families', views.FamilyViewSet, basename='family')
router.register(r'parents', views.ParentViewSet, basename='parent')
router.register(r'children', views.ChildViewSet, basename='child')
# Store endpoints moved to apps.store.urls
router.register(r'discounts', views.DiscountViewSet, basename='discount')
router.register(r'payments', views.PaymentViewSet, basename='payment')
router.register(r'recurring-payments', views.RecurringPaymentViewSet, basename='recurring-payment')
router.register(r'business-customers', views.BusinessCustomerViewSet, basename='business-customer')

urlpatterns = [
    path('', include(router.urls)),
    path('widget/lookup/', WidgetLookupView.as_view(), name='widget-lookup'),
    path('widget/register/', WidgetRegisterView.as_view(), name='widget-register'),
    path('widget/trial-register/', WidgetTrialRegisterView.as_view(), name='widget-trial-register'),
    path('widget/charge/', WidgetChargeView.as_view(), name='widget-charge'),
    path('widget/payment-status/', WidgetPaymentStatusView.as_view(), name='widget-payment-status'),
    path('widget/courses/', WidgetCoursesView.as_view(), name='widget-courses'),
    path('widget/course-types/', WidgetCourseTypesView.as_view(), name='widget-course-types'),
    path('widget/cities/', WidgetCitiesView.as_view(), name='widget-cities'),
    path('widget/branches/', WidgetBranchesView.as_view(), name='widget-branches'),
    path('widget/lesson-occurrences/', WidgetLessonOccurrencesView.as_view(), name='widget-lesson-occurrences'),
    path('widget/terms/', WidgetTermsView.as_view(), name='widget-terms'),
    path('card-update/<str:token>/', CardUpdatePreviewView.as_view(), name='card-update-preview'),
    path('card-update/<str:token>/charge/', CardUpdateChargeView.as_view(), name='card-update-charge'),
    path('card-links/', CardLinkListCreateView.as_view(), name='card-links'),
    path('card-links/options/', CardLinkOptionsView.as_view(), name='card-link-options'),
    path('invoices/<uuid:invoice_id>/pdf/', InvoicePdfView.as_view(), name='invoice-pdf'),
    path('card-links/<uuid:link_id>/<str:action>/', CardLinkActionView.as_view(), name='card-link-action'),
    path('card-link/<str:token>/', CardLinkPreviewView.as_view(), name='card-link-preview'),
    path('card-link/<str:token>/charge/', CardLinkChargeView.as_view(), name='card-link-charge'),
    path('cron/recurring-billing/', cron_recurring_billing, name='cron-recurring-billing'),
    path('cron/recurring-billing/status/', cron_recurring_billing_status, name='cron-recurring-billing-status'),
]

