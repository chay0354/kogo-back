from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.wahub import views, views_bot

router = SimpleRouter()
router.register(r'contacts', views.ContactViewSet, basename='wahub-contact')
router.register(r'tags', views.TagViewSet, basename='wahub-tag')
router.register(r'quick-replies', views.QuickReplyViewSet, basename='wahub-quick-reply')
# Stage 2 — the bot: knowledge, shadow replies, review (docs/WAHUB-CONTRACT-STAGE2.md).
router.register(r'knowledge', views_bot.KnowledgeViewSet, basename='wahub-knowledge')
router.register(r'shadow', views_bot.ShadowViewSet, basename='wahub-shadow')
router.register(r'review/proposals', views_bot.ReviewProposalViewSet, basename='wahub-review-proposal')

urlpatterns = [
    path('summary/', views.SummaryView.as_view(), name='wahub-summary'),
    path('status/', views.StatusView.as_view(), name='wahub-status'),
    path('settings/inbound-key/', views.InboundKeyView.as_view(), name='wahub-inbound-key'),
    path('inbound/manychat/', views.ManyChatInboundView.as_view(), name='wahub-inbound-manychat'),
    path('cron/tick/', views.cron_tick, name='wahub-cron-tick'),
    path('review/notes/', views_bot.ReviewNotesView.as_view(), name='wahub-review-notes'),
    path('review/summary/', views_bot.ReviewSummaryView.as_view(), name='wahub-review-summary'),
    path('demo/scenarios/', views.DemoScenariosView.as_view(), name='wahub-demo-scenarios'),
    path('demo/scenario/', views.DemoScenarioView.as_view(), name='wahub-demo-scenario'),
    path('demo/contacts/', views.DemoContactsView.as_view(), name='wahub-demo-contacts'),
    # A real conversation with a demo contact (docs/WAHUB-CONTRACT-REAL-DEMO.md): the new bot answers now;
    # a message's recording or picture, by a short-lived signed address.
    path('demo/contacts/<int:pk>/reply/', views.DemoReplyView.as_view(), name='wahub-demo-reply'),
    path('messages/<int:pk>/media/', views.MessageMediaView.as_view(), name='wahub-message-media'),
    # The live test from the owner's phone (docs/WAHUB-CONTRACT-LIVE-TEST.md, ב): the inbound door calls this
    # on itself, fire-and-forget, for a live-test contact's message; the same token as the cron.
    path('live-test/reply/', views.LiveTestReplyView.as_view(), name='wahub-live-test-reply'),
    # Stage 3 — who asked and did not register; the WhatsApp block of a customer's card (docs/WAHUB-CONTRACT-STAGE3.md).
    path('leads/unregistered/', views.UnregisteredLeadsView.as_view(), name='wahub-leads-unregistered'),
    path('for-customer/', views.ForCustomerView.as_view(), name='wahub-for-customer'),
    path('for-customer/recheck/', views.ForCustomerRecheckView.as_view(), name='wahub-for-customer-recheck'),
    path('', include(router.urls)),
]
