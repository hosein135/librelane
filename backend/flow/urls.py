from django.urls import path

from flow import views

urlpatterns = [
    path("", views.root_redirect, name="root"),
    path("login", views.login, name="login"),
    path("signup", views.signup, name="signup"),
    path("logout", views.logout, name="logout"),
    path("session/end", views.session_end, name="session_end"),
    path("api/session/ping", views.api_session_ping, name="api_session_ping"),
    path("api/home", views.api_home, name="api_home"),
    path("api/runs/new", views.create_run, name="create_run"),
    path("api/runs/<int:run_id>", views.run_detail, name="run_detail"),
    path("api/runs/<int:run_id>/setup", views.run_setup, name="run_setup"),
    path("api/runs/<int:run_id>/run-all", views.run_all, name="run_all"),
    path("api/runs/<int:run_id>/delete", views.delete_run, name="delete_run"),
    path(
        "api/runs/<int:run_id>/steps/<int:order>/run",
        views.run_step,
        name="run_step",
    ),
    path("api/runs/<int:run_id>/status", views.run_status, name="run_status"),
    # Keep legacy paths for download URLs used by the UI.
    path("runs/<int:run_id>/status.json", views.run_status, name="run_status_legacy"),
    path(
        "runs/<int:run_id>/steps/<int:order>/outputs.zip",
        views.download_step_zip,
        name="download_step_zip",
    ),
    path(
        "runs/<int:run_id>/steps/<int:order>/preview.svg",
        views.download_step_preview_svg,
        name="download_step_preview_svg",
    ),
    path(
        "runs/<int:run_id>/steps/<int:order>/preview-source",
        views.download_step_preview_source,
        name="download_step_preview_source",
    ),
]
