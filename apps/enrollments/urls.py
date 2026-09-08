from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'enrollments', views.EnrollmentViewSet, basename='enrollment')
router.register(r'lesson-enrollments', views.LessonEnrollmentViewSet, basename='lesson-enrollment')
router.register(r'trial-blocked-dates', views.TrialBlockedDateViewSet, basename='trial-blocked-date')

urlpatterns = [
    path('', include(router.urls)),
    path('cron/trial-reminders/', views.cron_trial_reminders, name='cron-trial-reminders'),
    path('cron/register-reminders/', views.cron_register_reminders, name='cron-register-reminders'),
    path('register-gaps/', views.register_gaps, name='register-gaps'),
    path('trial-registration-policy/', views.TrialRegistrationPolicyView.as_view(), name='trial-registration-policy'),
    path('trial-registration/lessons/', views.TrialRegistrationLessonsView.as_view(), name='trial-registration-lessons'),
]

