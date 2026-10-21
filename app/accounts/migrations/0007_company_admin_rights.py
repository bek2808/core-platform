"""
Администратор страховой: права на пользователей (часть A3, docs/users-model.md).

1. Поле «сменить пароль при входе» у пользователя: ставит система, когда
   выдаёт временный пароль; пока стоит - открывается только смена своего
   пароля (accounts/middleware.py). У нынешних пользователей - «нет».
2. Роль «Администратор страховой» получает права: пользователи
   и прикрепления (смотреть, заводить, менять; удаления нет), должности
   сотрудников (смотреть, добавлять), каталог ролей (смотреть). Ограничения
   (только своя страховая, не суперпользователи, свои роли не меняет,
   «последний администратор») - в экране и accounts/personnel.py.
3. Описание роли - что она теперь умеет.

Данные людей не меняются. Откат: права и описание роли - как были,
поле удаляется.
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations, models

ROLE_CODE = "company_admin"
NEW_PERMISSIONS = [
    ("accounts", "view_user"),
    ("accounts", "add_user"),
    ("accounts", "change_user"),
    ("accounts", "view_assignment"),
    ("accounts", "add_assignment"),
    ("accounts", "change_assignment"),
    ("accounts", "view_jobposition"),
    ("accounts", "add_jobposition"),
    ("accounts", "view_role"),
]
DESCRIPTION_BEFORE = (
    "Подразделения своей страховой (филиалы, отделы). Позже - пользователи, "
    "блокировка, сброс пароля, перевод (часть A3)."
)
DESCRIPTION_AFTER = (
    "Пользователи и подразделения своей страховой: заводит, принимает, переводит, увольняет, "
    "блокирует, сбрасывает пароль; назначает роли сотрудникам; добавляет должности. "
    "Свои роли и место работы не меняет; суперпользователей не видит."
)


def permissions(apps):
    app_config = apps.get_app_config("accounts")
    app_config.models_module = True
    create_permissions(app_config, apps=apps, verbosity=0)
    app_config.models_module = None
    Permission = apps.get_model("auth", "Permission")
    found = []
    for app_label, codename in NEW_PERMISSIONS:
        permission = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
        if permission is None:
            raise RuntimeError(f"Нет права {app_label}.{codename} - права администратора страховой не выданы.")
        found.append(permission)
    return found


def give_rights(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    role = Role.objects.select_related("group").get(code=ROLE_CODE)
    role.group.permissions.add(*permissions(apps))
    role.description = DESCRIPTION_AFTER
    role.save(update_fields=["description"])


def take_rights(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    role = Role.objects.select_related("group").get(code=ROLE_CODE)
    role.group.permissions.remove(*permissions(apps))
    role.description = DESCRIPTION_BEFORE
    role.save(update_fields=["description"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0006_rights_from_assignments"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="must_change_password",
            field=models.BooleanField(
                default=False,
                help_text="Ставит система, когда выдаёт временный пароль (новый пользователь, «Сбросить пароль»). Пока человек не задаст свой пароль, другие страницы не открываются.",
                verbose_name="сменить пароль при входе",
            ),
        ),
        migrations.RunPython(give_rights, take_rights),
    ]
