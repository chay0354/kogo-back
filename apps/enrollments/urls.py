from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'enrollments', views.EnrollmentViewSet, basename='enrollment')
router.register(r'lesson-enrollments', views.LessonEnrollmentViewSet, basename='lesson-enrollment')

urlpatterns = [
    path('', include(router.urls)),
    path('cron/trial-reminders/', views.cron_trial_reminders, name='cron-trial-reminders'),
]

