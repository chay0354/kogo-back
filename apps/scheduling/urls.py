from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import LessonViewSet, ScheduleEventViewSet

router = DefaultRouter()
router.register(r'lessons', LessonViewSet, basename='lesson')
router.register(r'events', ScheduleEventViewSet, basename='event')

urlpatterns = [
    path('', include(router.urls)),
]
