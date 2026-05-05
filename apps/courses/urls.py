from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'types', views.CourseTypeViewSet, basename='coursetype')
router.register(r'courses', views.CourseViewSet, basename='course')
router.register(r'lessons', views.LessonViewSet, basename='lesson')

# Legacy routes for backward compatibility
router.register(r'course-list', views.CourseListViewSet, basename='course-list')
router.register(r'lesson-list', views.LessonListViewSet, basename='lesson-list')

urlpatterns = [
    path('', include(router.urls)),
]

