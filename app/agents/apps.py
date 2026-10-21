from django.apps import AppConfig


class AgentsConfig(AppConfig):
    name = "agents"
    verbose_name = "Агенты"

    def ready(self):
        # Журнал изменений скрывает агентов, которых человек не видит (часть B4).
        from accounts.visibility import hide_in_journal

        from .models import journal_hidden

        hide_in_journal(journal_hidden)
