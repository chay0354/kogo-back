from django.urls import path

from apps.core.auth_views import (
    CompleteTourView,
    ForgotPasswordView,
    LinkedUsersView,
    LoginView,
    LogoutView,
    MeView,
    ResetPasswordView,
)
from apps.core.integration_views import IntegrationCredentialView


urlpatterns = [
    path('login/', LoginView.as_view(), name='auth-login'),
    path('logout/', LogoutView.as_view(), name='auth-logout'),
    path('me/', MeView.as_view(), name='auth-me'),
    path('complete-tour/', CompleteTourView.as_view(), name='auth-complete-tour'),
    path('linked-users/', LinkedUsersView.as_view(), name='auth-linked-users'),
    path(
        'integration-credentials/',
        IntegrationCredentialView.as_view(),
        name='auth-integration-credentials',
    ),
    path('forgot-password/', ForgotPasswordView.as_view(), name='auth-forgot-password'),
    path('reset-password/', ResetPasswordView.as_view(), name='auth-reset-password'),
]


