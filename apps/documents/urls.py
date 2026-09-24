from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.documents.signing import views as signing_views
from apps.documents.views import (
    CashPlanViewSet,
    CheckPlanViewSet,
    DocumentSeriesViewSet,
    FormalDocumentViewSet,
    MissingReceiptsViewSet,
)

router = DefaultRouter()
router.register(r'documents', FormalDocumentViewSet, basename='document')
router.register(r'check-plans', CheckPlanViewSet, basename='check-plan')
router.register(r'cash-plans', CashPlanViewSet, basename='cash-plan')
router.register(r'missing-receipts', MissingReceiptsViewSet, basename='missing-receipts')
router.register(r'series', DocumentSeriesViewSet, basename='document-series')

urlpatterns = [
    # Signed originals (apps/documents/signing).
    path('signing/status/', signing_views.signing_status, name='signing-status'),
    path('signing/originals/', signing_views.signed_originals, name='signing-originals'),
    path('signing/originals/<uuid:original_id>/print-original/', signing_views.print_original,
         name='signing-print-original'),
    path('signing/certificate/', signing_views.signing_certificate, name='signing-certificate'),
    path('signing/certificate/issue/', signing_views.signing_issue_certificate, name='signing-certificate-issue'),
    path('signing/selftest/', signing_views.signing_selftest, name='signing-selftest'),
    path('cron/sign-pending/', signing_views.cron_sign_pending, name='documents-cron-sign-pending'),
    path('', include(router.urls)),
]
