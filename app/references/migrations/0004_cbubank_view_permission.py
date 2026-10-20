"""
Право «просмотр банков по перечню ЦБ» - группам «Администратор справочников»
и «Сотрудник» (группы созданы миграцией accounts/0003), как у справочников
ЕРСП. Править перечень не может никто: он меняется только загрузкой.

Таблиц не меняет. Откат снимает право с обеих групп.
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

GROUPS = ["Администратор справочников", "Сотрудник"]
CODENAME = "view_cbubank"


def grant(apps, schema_editor):
    # На чистой базе права появляются только после всех миграций -
    # создаём их заранее (как в accounts/0003).
    app_config = apps.get_app_config("references")
    app_config.models_module = True
    create_permissions(app_config, apps=apps, verbosity=0)
    app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    permission = Permission.objects.get(content_type__app_label="references", codename=CODENAME)
    for group in Group.objects.filter(name__in=GROUPS):
        group.permissions.add(permission)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    permission = Permission.objects.filter(content_type__app_label="references", codename=CODENAME).first()
    if permission is not None:
        for group in Group.objects.filter(name__in=GROUPS):
            group.permissions.remove(permission)


class Migration(migrations.Migration):

    dependencies = [
        ("references", "0003_cbu_banks"),
        ("accounts", "0003_access_groups"),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
