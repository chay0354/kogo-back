"""
API URL configuration
"""
from django.urls import path, include

urlpatterns = [
    path('core/', include('apps.core.urls')),
    # path('branches/', include('apps.branches.urls')),  # REMOVED - duplicate of core/branches
    path('instructors/', include('apps.instructors.urls')),
    path('courses/', include('apps.courses.urls')),
    path('customers/', include('apps.customers.urls')),
    path('enrollments/', include('apps.enrollments.urls')),
    path('scheduling/', include('apps.scheduling.urls')),
    path('store/', include('apps.store.urls')),
    path('documents/', include('apps.documents.urls')),
]

