"""
Роль «Администратор платформы» (часть A4, docs/users-model.md).

Администратор платформы - учётная запись вида «платформа» без страховой.
Роль даётся группой в учётной записи (у платформы нет прикреплений);
завести такого человека и дать ему группу может только суперпользователь.

Права: страховые компании (смотреть, заводить, менять), пользователи
(смотреть, заводить, менять - в пределах правил экрана: из людей страховых
только их администраторы), должности сотрудников (смотреть, добавлять,
переименовывать, снимать), каталог ролей, справочники страховщиков
и регионов ЕРСП (выбор в карточке страховой), журнал изменений (только
таблицы платформы). Данных страховых (контрагенты, обмен с ЕРСП) нет.

Данные людей не меняются. Откат: роль и группа удаляются.
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

ROLE_CODE = "platform_admin"
GROUP_NAME = "Администратор платформы"
DESCRIPTION = (
    "Страховые компании и их администраторы: заводит страховую и её первого администратора, "
    "блокирует администраторов страховых и сбрасывает им пароль, видит список пользователей "
    "всех страховых и состояние (счётчики, последний вход). Справочник должностей сотрудников. "
    "Данных страховых (клиенты, договоры, обмен с ЕРСП) не видит."
)
PERMISSIONS = [
    ("companies", "view_insurancecompany"),
    ("companies", "add_insurancecompany"),
    ("companies", "change_insurancecompany"),
    ("accounts", "view_user"),
    ("accounts", "add_user"),
    ("accounts", "change_user"),
    ("accounts", "view_jobposition"),
    ("accounts", "add_jobposition"),
    ("accounts", "change_jobposition"),
    ("accounts", "view_role"),
    ("references", "view_erspinsurer"),
    ("references", "view_erspregion"),
    ("audit", "view_changelog"),
]


def add_role(apps, schema_editor):
    for label in ("companies", "accounts", "references", "audit"):
        app_config = apps.get_app_config(label)
        app_config.models_module = True
        create_permissions(app_config, apps=apps, verbosity=0)
        app_config.models_module = None
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    Role = apps.get_model("accounts", "Role")
    found = []
    for app_label, codename in PERMISSIONS:
        permission = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
        if permission is None:
            raise RuntimeError(f"Нет права {app_label}.{codename} - роль администратора платформы не заведена.")
        found.append(permission)
    group, _ = Group.objects.get_or_create(name=GROUP_NAME)
    group.permissions.set(found)
    Role.objects.update_or_create(
        code=ROLE_CODE,
        defaults={"group": group, "audience": "platform", "default_visibility": "",
                  "description": DESCRIPTION, "sort_order": 900},
    )


def remove_role(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Role = apps.get_model("accounts", "Role")
    Role.objects.filter(code=ROLE_CODE).delete()
    Group.objects.filter(name=GROUP_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0007_company_admin_rights"),
        ("companies", "0004_company_journal"),
    ]

    operations = [
        migrations.RunPython(add_role, remove_role),
    ]
