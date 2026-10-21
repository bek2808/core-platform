"""
Права на данные физлица (counterparties.Individual).

- «Администратор справочников» - смотреть, добавлять, менять (как данные юрлица);
- «Сотрудник» - только смотреть.
Удалять не может никто (удаление в системе выключено для всех). Роль «Работа
с агентами» получает просмотр в agents/0009: её группа создаётся в приложении
агентов. Таблиц не меняет. Откат снимает эти права (прямым SQL).
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

ADMIN_GROUP = "Администратор справочников"
EMPLOYEE_GROUP = "Сотрудник"
RIGHTS = {
    ADMIN_GROUP: ["add_individual", "change_individual", "view_individual"],
    EMPLOYEE_GROUP: ["view_individual"],
}


def give(apps, schema_editor):
    # На чистой базе права появляются только после всех миграций - создаём заранее.
    app_config = apps.get_app_config("counterparties")
    app_config.models_module = True
    create_permissions(app_config, apps=apps, verbosity=0)
    app_config.models_module = None
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    for name, codenames in RIGHTS.items():
        group = Group.objects.get(name=name)
        group.permissions.add(*Permission.objects.filter(
            content_type__app_label="counterparties", codename__in=codenames,
        ))


def take_back(apps, schema_editor):
    # Прямым SQL: через модели Django откат путает версии моделей прав и групп.
    with schema_editor.connection.cursor() as cursor:
        for name, codenames in RIGHTS.items():
            cursor.execute(
                """
                DELETE FROM auth_group_permissions
                WHERE group_id = (SELECT id FROM auth_group WHERE name = %s)
                  AND permission_id IN (
                      SELECT p.id FROM auth_permission p JOIN django_content_type ct ON ct.id = p.content_type_id
                      WHERE ct.app_label = 'counterparties' AND p.codename = ANY(%s))
                """,
                [name, codenames],
            )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_access_groups"),
        ("counterparties", "0007_individual"),
    ]

    operations = [
        migrations.RunPython(give, take_back),
    ]
