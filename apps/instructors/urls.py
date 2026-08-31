from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'', views.InstructorViewSet, basename='instructor')
router.register(r'bonuses', views.InstructorBonusViewSet, basename='instructor-bonus')

urlpatterns = [
    # Declared before the router: the instructor router is registered on the
    # empty prefix, so its detail route would otherwise swallow this path.
    path('my-branches/', views.MyBranchesView.as_view(), name='instructor-my-branches'),
    path('my-dashboard/', views.MyDashboardView.as_view(), name='instructor-my-dashboard'),
    path('', include(router.urls)),
]

