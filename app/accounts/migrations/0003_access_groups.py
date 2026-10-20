"""
Группы доступа к справочнику контрагентов (модель: docs/counterparties-model.md, «Права»).

«Администратор справочников» - заводит и правит контрагентов (карточка,
данные юрлица, представители, счета), банки, должности, основания
полномочий; переводит контрагентов в архив. Справочники ЕРСП и журнал
изменений - только просмотр. Удалять не может ничего (удаление в системе
выключено для всех).

«Сотрудник» - только просмотр того же. Решение Бекзода 20.10: группа
создаётся сейчас, но пока пустая; создание контрагента из формы полиса
и «дописать пустые поля» появятся вместе с полисами.

Чтобы человек вошёл в админ-панель, кроме группы ему нужна отметка
«статус персонала» (is_staff) и страховая компания в учётной записи.
Суперпользователю группы не нужны: у него все права.

Откат удаляет обе группы (участники теряют эти права).
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

ADMIN_GROUP = "Администратор справочников"
EMPLOYEE_GROUP = "Сотрудник"

# Контрагенты и наши справочники для ввода.
COUNTERPARTY_MODELS = [
    "counterparty", "legalentity", "representative", "bankaccount",
    "bank", "position", "powerbasis",
]
# Справочники ЕРСП: правятся только загрузкой, людям - просмотр.
ERSP_MODELS = [
    "erspregion", "erspdistrict", "erspownershipform", "erspinsurer",
    "erspcurrency", "erspoked",
]


def codenames(actions, models):
    return [f"{action}_{model}" for model in models for action in actions]


ADMIN_PERMISSIONS = (
    [("counterparties", c) for c in codenames(["add", "change", "view"], COUNTERPARTY_MODELS)]
    + [("references", c) for c in codenames(["view"], ERSP_MODELS)]
    + [("audit", "view_changelog")]
)
EMPLOYEE_PERMISSIONS = (
    [("counterparties", c) for c in codenames(["view"], COUNTERPARTY_MODELS)]
    + [("references", c) for c in codenames(["view"], ERSP_MODELS)]
    + [("audit", "view_changelog")]
)


def add_groups(apps, schema_editor):
    # На чистой базе (например, в автотестах) права создаются только после
    # всех миграций. Создаём их здесь заранее - иначе группам нечего выдать.
    for app_config in apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, apps=apps, verbosity=0)
        app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    for name, wanted in [(ADMIN_GROUP, ADMIN_PERMISSIONS), (EMPLOYEE_GROUP, EMPLOYEE_PERMISSIONS)]:
        group, _ = Group.objects.get_or_create(name=name)
        permissions = []
        for app_label, codename in wanted:
            permission = Permission.objects.filter(
                content_type__app_label=app_label, codename=codename
            ).first()
            if permission is None:
                raise RuntimeError(f"Нет права {app_label}.{codename} - группа «{name}» не создана.")
            permissions.append(permission)
        group.permissions.set(permissions)


def remove_groups(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name__in=[ADMIN_GROUP, EMPLOYEE_GROUP]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_user_insurance_company"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("audit", "0002_changelog_card"),
        ("counterparties", "0006_representative_form_labels"),
        ("references", "0002_ersp_districts_currencies_okeds"),
    ]

    operations = [
        migrations.RunPython(add_groups, remove_groups),
    ]
