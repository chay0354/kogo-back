from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.external_students import views

router = DefaultRouter()
router.register(r'students', views.ExternalStudentViewSet, basename='external-student')

urlpatterns = [
    path('', include(router.urls)),
]
