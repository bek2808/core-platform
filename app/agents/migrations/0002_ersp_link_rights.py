"""
Права на справочник агентов ЕРСП и связи с ЕРСП (часть B1).

- Роль «Администратор страховой»: связи с ЕРСП - смотреть, заводить,
  менять (снять отметку «действует»); справочник агентов ЕРСП - смотреть.
  Связь заводится на экране подразделения, как и само подразделение.
- Группа «Администратор интеграций»: справочник агентов ЕРСП - смотреть
  (она ведёт подключение к ЕРСП и видит обмен с ним).

Удаления нет ни у кого. Администратор платформы этих прав не получает:
данные страховых ему не видны.

Таблиц не меняет. Откат снимает эти права.
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

COMPANY_ADMIN_ROLE = "company_admin"
INTEGRATIONS_GROUP = "Администратор интеграций"
COMPANY_ADMIN_PERMISSIONS = [
    ("agents", "view_ersplink"),
    ("agents", "add_ersplink"),
    ("agents", "change_ersplink"),
    ("integrations", "view_erspagency"),
]
INTEGRATIONS_PERMISSIONS = [("integrations", "view_erspagency")]


def find(apps, wanted):
    # На чистой базе права появляются только после всех миграций -
    # создаём их заранее (как в accounts/0003).
    for label in ("agents", "integrations"):
        app_config = apps.get_app_config(label)
        app_config.models_module = True
        create_permissions(app_config, apps=apps, verbosity=0)
        app_config.models_module = None
    Permission = apps.get_model("auth", "Permission")
    found = []
    for app_label, codename in wanted:
        permission = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
        if permission is None:
            raise RuntimeError(f"Нет права {app_label}.{codename} - миграция остановлена.")
        found.append(permission)
    return found


def groups(apps):
    Role = apps.get_model("accounts", "Role")
    Group = apps.get_model("auth", "Group")
    return (
        Role.objects.select_related("group").get(code=COMPANY_ADMIN_ROLE).group,
        Group.objects.filter(name=INTEGRATIONS_GROUP).first(),
    )


def grant(apps, schema_editor):
    company_admin, integrations = groups(apps)
    company_admin.permissions.add(*find(apps, COMPANY_ADMIN_PERMISSIONS))
    if integrations is not None:
        integrations.permissions.add(*find(apps, INTEGRATIONS_PERMISSIONS))


def revoke(apps, schema_editor):
    company_admin, integrations = groups(apps)
    company_admin.permissions.remove(*find(apps, COMPANY_ADMIN_PERMISSIONS))
    if integrations is not None:
        integrations.permissions.remove(*find(apps, INTEGRATIONS_PERMISSIONS))


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0001_ersp_link"),
        ("accounts", "0008_platform_admin"),
        ("integrations", "0004_integrations_admin_group"),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
