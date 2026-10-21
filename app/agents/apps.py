from django.apps import AppConfig

# Назначение файла «скан агентского соглашения» (files.StoredFile.purpose).
AGREEMENT_SCAN = "agreement_scan"


class AgentsConfig(AppConfig):
    name = "agents"
    verbose_name = "Агенты"

    def ready(self):
        # Журнал изменений скрывает агентов, которых человек не видит (часть B4).
        from accounts.visibility import hide_in_journal

        from .models import journal_hidden

        hide_in_journal(journal_hidden)

        # Скан агентского соглашения (хранилище файлов, docs/files-model.md):
        # только PDF до 5 МБ - так требует ЕРСП. Файл видит тот, кто видит агента.
        from files import registry

        from .models import Agreement, visible_agents

        registry.register(registry.Purpose(
            code=AGREEMENT_SCAN,
            label="скан агентского соглашения",
            types=frozenset({registry.PDF}),
            max_size=5 * registry.MB,
            owners=lambda scope: Agreement.objects.filter(agent__in=visible_agents(scope)),
            view_perm="agents.view_agreement",
        ))
