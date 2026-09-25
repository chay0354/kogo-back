from django.apps import AppConfig


class InstructorsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.instructors'

    def ready(self):
        from apps.instructors import group_freshness

        group_freshness.connect()
