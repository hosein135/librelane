from django.urls import path

from flow import views

urlpatterns = [
    path("", views.home, name="home"),
    path("runs/new/", views.create_run, name="create_run"),
    path("runs/<int:run_id>/", views.run_detail, name="run_detail"),
    path("runs/<int:run_id>/setup/", views.run_setup, name="run_setup"),
    path("runs/<int:run_id>/run-all/", views.run_all, name="run_all"),
    path("runs/<int:run_id>/delete/", views.delete_run, name="delete_run"),
    path(
        "runs/<int:run_id>/steps/<int:order>/run/",
        views.run_step,
        name="run_step",
    ),
    path("runs/<int:run_id>/status.json", views.run_status, name="run_status"),
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
