from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.documents.views import FormalDocumentViewSet

router = DefaultRouter()
router.register(r'documents', FormalDocumentViewSet, basename='document')

urlpatterns = [
    path('', include(router.urls)),
]
