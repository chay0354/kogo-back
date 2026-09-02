from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.documents.views import FormalDocumentViewSet, CheckPlanViewSet

router = DefaultRouter()
router.register(r'documents', FormalDocumentViewSet, basename='document')
router.register(r'check-plans', CheckPlanViewSet, basename='check-plan')

urlpatterns = [
    path('', include(router.urls)),
]
