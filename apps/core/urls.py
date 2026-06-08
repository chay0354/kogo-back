from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views
from . import auth_views
from . import dashboard_views
from . import partner_views
from .manychat_views import WhatsAppViewSet

router = DefaultRouter()
router.register(r'cities', views.CityViewSet, basename='city')
router.register(r'branches', views.BranchViewSet, basename='branch')
router.register(r'rooms', views.RoomViewSet, basename='room')
router.register(r'branch-files', views.BranchFileViewSet, basename='branch-file')
router.register(r'users', auth_views.UserViewSet, basename='user')
router.register(r'partners', partner_views.PartnerViewSet, basename='partner')
router.register(r'dashboard', dashboard_views.DashboardViewSet, basename='dashboard')
router.register(r'whatsapp', WhatsAppViewSet, basename='whatsapp')

urlpatterns = [
    path('', include(router.urls)),
    path('auth/', include('apps.core.auth_urls')),
]

