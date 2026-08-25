from django.apps import AppConfig


class FlowConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "flow"

    def ready(self) -> None:
        from librelane_web.settings import ensure_data_dirs

        ensure_data_dirs()
