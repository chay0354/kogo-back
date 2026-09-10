from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.external_students import import_views, views

router = DefaultRouter()
router.register(r'students', views.ExternalStudentViewSet, basename='external-student')
router.register(r'imports', import_views.ExternalRosterImportViewSet, basename='external-roster-import')

urlpatterns = [
    path('', include(router.urls)),
    path('cron/roster-imports/', import_views.cron_roster_imports, name='cron-roster-imports'),
]
