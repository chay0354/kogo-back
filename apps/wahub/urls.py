from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.wahub import views

router = SimpleRouter()
router.register(r'contacts', views.ContactViewSet, basename='wahub-contact')
router.register(r'tags', views.TagViewSet, basename='wahub-tag')
router.register(r'quick-replies', views.QuickReplyViewSet, basename='wahub-quick-reply')

urlpatterns = [
    path('summary/', views.SummaryView.as_view(), name='wahub-summary'),
    path('status/', views.StatusView.as_view(), name='wahub-status'),
    path('settings/inbound-key/', views.InboundKeyView.as_view(), name='wahub-inbound-key'),
    path('inbound/manychat/', views.ManyChatInboundView.as_view(), name='wahub-inbound-manychat'),
    path('cron/tick/', views.cron_tick, name='wahub-cron-tick'),
    path('', include(router.urls)),
]
