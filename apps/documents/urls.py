from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.documents.views import FormalDocumentViewSet, CheckPlanViewSet, MissingReceiptsViewSet

router = DefaultRouter()
router.register(r'documents', FormalDocumentViewSet, basename='document')
router.register(r'check-plans', CheckPlanViewSet, basename='check-plan')
router.register(r'missing-receipts', MissingReceiptsViewSet, basename='missing-receipts')

urlpatterns = [
    path('', include(router.urls)),
]
