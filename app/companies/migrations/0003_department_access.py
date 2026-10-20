"""
Права на подразделения (модель: docs/users-model.md, «Роль»).

«Администратор страховой» - новая группа: заводит и правит подразделения
своей страховой (филиалы, отделы), закрывает их; видит журнал изменений.
В части A2 сюда добавятся пользователи и прикрепления, а группа станет
ролью «Администратор страховой».

«Сотрудник» (группа из accounts/0003) - дополнительно видит подразделения
своей страховой: структура компании не секрет для её сотрудников.

Чтобы человек вошёл в админ-панель, кроме группы ему нужна отметка
«статус персонала» (is_staff) и страховая компания в учётной записи.

Миграция не меняет таблиц. Откат удаляет группу «Администратор страховой»
и снимает просмотр подразделений с группы «Сотрудник».
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

COMPANY_ADMIN_GROUP = "Администратор страховой"
EMPLOYEE_GROUP = "Сотрудник"

COMPANY_ADMIN_PERMISSIONS = [
    ("companies", "add_department"),
    ("companies", "change_department"),
    ("companies", "view_department"),
    # Выбор региона филиала - поиском по справочнику регионов ЕРСП.
    ("references", "view_erspregion"),
    ("audit", "view_changelog"),
]
EMPLOYEE_EXTRA = ("companies", "view_department")


def find_permission(Permission, app_label, codename):
    permission = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
    if permission is None:
        raise RuntimeError(f"Нет права {app_label}.{codename} - права на подразделения не выданы.")
    return permission


def add_access(apps, schema_editor):
    # На чистой базе права появляются только после всех миграций -
    # создаём их заранее (как в accounts/0003).
    for label in ("companies", "references", "audit"):
        app_config = apps.get_app_config(label)
        app_config.models_module = True
        create_permissions(app_config, apps=apps, verbosity=0)
        app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    group, _ = Group.objects.get_or_create(name=COMPANY_ADMIN_GROUP)
    group.permissions.set([find_permission(Permission, *item) for item in COMPANY_ADMIN_PERMISSIONS])

    employees = Group.objects.filter(name=EMPLOYEE_GROUP).first()
    if employees is None:
        raise RuntimeError(f"Нет группы «{EMPLOYEE_GROUP}» (её создаёт accounts/0003).")
    employees.permissions.add(find_permission(Permission, *EMPLOYEE_EXTRA))


def remove_access(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    Group.objects.filter(name=COMPANY_ADMIN_GROUP).delete()
    employees = Group.objects.filter(name=EMPLOYEE_GROUP).first()
    permission = Permission.objects.filter(
        content_type__app_label=EMPLOYEE_EXTRA[0], codename=EMPLOYEE_EXTRA[1]
    ).first()
    if employees is not None and permission is not None:
        employees.permissions.remove(permission)


class Migration(migrations.Migration):

    dependencies = [
        ("companies", "0002_department"),
        ("accounts", "0003_access_groups"),
        ("audit", "0003_changelog_source_cbu"),
        ("references", "0004_cbubank_view_permission"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(add_access, remove_access),
    ]
