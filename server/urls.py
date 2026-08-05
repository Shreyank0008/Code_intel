from django.urls import path, re_path
from api import views

urlpatterns = [
    path("", views.index),
    path("api/config", views.config),
    path("api/examples", views.examples),
    path("api/settings", views.settings_api),
    path("api/settings/test", views.settings_test),
    path("api/agents", views.agents_list),
    path("api/agents/<str:aid>", views.agent_update),
    path("api/ingest", views.ingest),
    path("api/jobs", views.job_list),
    path("api/jobs/<str:jid>/messages", views.chat_messages),
    path("api/jobs/<str:jid>/chat", views.chat_send),
    path("api/jobs/<str:jid>/chat/answer", views.chat_answer),
    path("api/jobs/<str:jid>/chat/action", views.chat_action),
    path("api/jobs/<str:jid>", views.job_detail),
    path("api/jobs/<str:jid>/logs", views.job_logs),
    path("api/jobs/<str:jid>/revive/start", views.revive_start),
    path("api/jobs/<str:jid>/revive/status", views.revive_status),
    path("api/jobs/<str:jid>/revive/stop", views.revive_stop),
    path("api/jobs/<str:jid>/continue", views.mark_continued),
    path("api/jobs/<str:jid>/finalise", views.finalise),
    path("api/jobs/<str:jid>/build/<str:kind>", views.stage2_build),
    path("api/jobs/<str:jid>/build/<str:kind>/run", views.stage2_execute),
    path("api/jobs/<str:jid>/build/<str:kind>/status", views.stage2_status),
    # SPA client-side routes — serve index.html for any non-API path
    re_path(r"^(?!api/|static/).*$", views.index),
]
