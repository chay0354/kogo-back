from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'', views.InstructorViewSet, basename='instructor')
router.register(r'bonuses', views.InstructorBonusViewSet, basename='instructor-bonus')

urlpatterns = [
    path('', include(router.urls)),
]

