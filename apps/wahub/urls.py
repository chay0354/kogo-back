from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.wahub import views, views_bot, views_leads, views_mode

router = SimpleRouter()
router.register(r'contacts', views.ContactViewSet, basename='wahub-contact')
router.register(r'tags', views.TagViewSet, basename='wahub-tag')
router.register(r'quick-replies', views.QuickReplyViewSet, basename='wahub-quick-reply')
# Stage 2 — the bot: knowledge, shadow replies, review (docs/WAHUB-CONTRACT-STAGE2.md).
router.register(r'knowledge', views_bot.KnowledgeViewSet, basename='wahub-knowledge')
router.register(r'shadow', views_bot.ShadowViewSet, basename='wahub-shadow')
router.register(r'review/proposals', views_bot.ReviewProposalViewSet, basename='wahub-review-proposal')
# "דיוק" — fixing the bot in free text (docs/WAHUB-CONTRACT-REFINE.md).
router.register(r'refine', views_bot.RefineViewSet, basename='wahub-refine')

urlpatterns = [
    path('summary/', views.SummaryView.as_view(), name='wahub-summary'),
    path('status/', views.StatusView.as_view(), name='wahub-status'),
    path('settings/inbound-key/', views.InboundKeyView.as_view(), name='wahub-inbound-key'),
    path('settings/review/', views_bot.ReviewSettingsView.as_view(), name='wahub-settings-review'),
    # Whom the NEW bot answers for real — the test numbers or everybody — and whether its follow-ups to leads may
    # leave (docs/WAHUB-CONTRACT-GO-LIVE.md, ג.1). Both are off until a manager turns them on.
    path('settings/bot-mode/', views_mode.BotModeView.as_view(), name='wahub-settings-bot-mode'),
    path('settings/followups/', views_mode.BotFollowupsView.as_view(), name='wahub-settings-followups'),
    path('inbound/manychat/', views.ManyChatInboundView.as_view(), name='wahub-inbound-manychat'),
    path('cron/tick/', views.cron_tick, name='wahub-cron-tick'),
    path('review/notes/', views_bot.ReviewNotesView.as_view(), name='wahub-review-notes'),
    path('review/summary/', views_bot.ReviewSummaryView.as_view(), name='wahub-review-summary'),
    # "בדיקות הבוט" — the bot's self-checks (docs/WAHUB-CONTRACT-BOT-EVALS.md): the list with the last results,
    # one conversation played per request, and one kept run in full.
    path('bot/evals/', views_bot.BotEvalsView.as_view(), name='wahub-bot-evals'),
    path('bot/evals/run/', views_bot.BotEvalRunView.as_view(), name='wahub-bot-evals-run'),
    path('bot/evals/runs/<int:pk>/', views_bot.BotEvalRunDetailView.as_view(), name='wahub-bot-evals-run-detail'),
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
    path('media/prepare/', views.MediaPrepareView.as_view(), name='wahub-media-prepare'),
    # Stage 3 — who asked and did not register; the WhatsApp block of a customer's card (docs/WAHUB-CONTRACT-STAGE3.md).
    path('leads/unregistered/', views.UnregisteredLeadsView.as_view(), name='wahub-leads-unregistered'),
    # The lead stages (docs/WAHUB-CONTRACT-LEAD-STAGES.md): how many contacts are in each; the lists are contacts/?lead_stage=.
    path('leads/board/', views.LeadBoardView.as_view(), name='wahub-leads-board'),
    # Leads, version 2 (docs/WAHUB-CONTRACT-LEADS-V2.md, ד): the lists by who makes the next move, one lead in
    # full, and what a person does to a lead. The board above stays for the screen that still reads it.
    path('leads/', views_leads.LeadListView.as_view(), name='wahub-leads'),
    path('leads/templates/', views_leads.LeadTemplatesView.as_view(), name='wahub-leads-templates'),
    # "חזרה אוטומטית ללידים" (the same contract, י): what is missing for the follow-ups to leave by themselves,
    # the automations ManyChat has, and connecting one to a follow-up. `options/` before the key: it is no key.
    path('leads/automation/', views_leads.LeadAutomationView.as_view(), name='wahub-leads-automation'),
    path('leads/templates/options/', views_leads.LeadTemplateOptionsView.as_view(), name='wahub-leads-template-options'),
    path('leads/templates/<slug:template_key>/', views_leads.LeadTemplateView.as_view(), name='wahub-leads-template'),
    path('leads/<int:pk>/', views_leads.LeadDetailView.as_view(), name='wahub-lead'),
    path('leads/<int:pk>/close/', views_leads.LeadCloseView.as_view(), name='wahub-lead-close'),
    path('leads/<int:pk>/reopen/', views_leads.LeadReopenView.as_view(), name='wahub-lead-reopen'),
    path('leads/<int:pk>/snooze/', views_leads.LeadSnoozeView.as_view(), name='wahub-lead-snooze'),
    path('leads/<int:pk>/owner/', views_leads.LeadOwnerView.as_view(), name='wahub-lead-owner'),
    # The waiting list of a lesson that is full (docs/WAHUB-CONTRACT-FUTURE-LEADS.md, ד): who waits for what, and
    # the two things the office does to a row. Putting somebody on a list is contacts/{id}/waiting-list/.
    path('waiting-list/', views.WaitingListView.as_view(), name='wahub-waiting-list'),
    # The same contract, ז: the lessons a person chooses from, and telling one row's customer that a seat
    # came free — GET shows what the press would do, POST is the press.
    path('waiting-list/lessons/', views.WaitingListLessonsView.as_view(), name='wahub-waiting-list-lessons'),
    path('waiting-list/<int:pk>/notify/', views.WaitingListNotifyView.as_view(), name='wahub-waiting-list-notify'),
    path('waiting-list/<int:pk>/handled/', views.WaitingListHandledView.as_view(), name='wahub-waiting-list-handled'),
    path('waiting-list/<int:pk>/remove/', views.WaitingListRemoveView.as_view(), name='wahub-waiting-list-remove'),
    path('for-customer/', views.ForCustomerView.as_view(), name='wahub-for-customer'),
    path('for-customer/recheck/', views.ForCustomerRecheckView.as_view(), name='wahub-for-customer-recheck'),
    path('', include(router.urls)),
]
