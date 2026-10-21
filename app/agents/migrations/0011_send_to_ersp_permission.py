"""
Право «Может отправлять агентов в ЕРСП» (решение Бекзода 22.10: отправляет
только «Администратор страховой»). Роль «Работа с агентами» этого права
не получает. Таблиц не меняет. Откат снимает право с администратора
страховой (прямым SQL).
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

COMPANY_ADMIN_ROLE = "company_admin"
CODENAME = "send_to_ersp"


def give(apps, schema_editor):
    # На чистой базе права появляются после всех миграций - создаём заранее.
    app_config = apps.get_app_config("agents")
    app_config.models_module = True
    create_permissions(app_config, apps=apps, verbosity=0)
    app_config.models_module = None
    Role = apps.get_model("accounts", "Role")
    Permission = apps.get_model("auth", "Permission")
    group = Role.objects.select_related("group").get(code=COMPANY_ADMIN_ROLE).group
    group.permissions.add(Permission.objects.get(content_type__app_label="agents", codename=CODENAME))


def take_back(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM auth_group_permissions
            WHERE group_id = (SELECT group_id FROM accounts_role WHERE code = %s)
              AND permission_id IN (
                  SELECT p.id FROM auth_permission p JOIN django_content_type ct ON ct.id = p.content_type_id
                  WHERE ct.app_label = 'agents' AND p.codename = %s)
            """,
            [COMPANY_ADMIN_ROLE, CODENAME],
        )


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0010_agent_formation_date_help"),
        ("accounts", "0008_platform_admin"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="agent",
            options={
                "ordering": ["counterparty__display_name"],
                "permissions": [("send_to_ersp", "Может отправлять агентов в ЕРСП")],
                "verbose_name": "агент",
                "verbose_name_plural": "агенты",
            },
        ),
        migrations.RunPython(give, take_back),
    ]
