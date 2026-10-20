"""
Право «просмотр журнала обмена» - группам «Администратор справочников»
и «Сотрудник» (группы созданы миграцией accounts/0003).

Журнал обмена только для чтения у всех: записывает его только сама
система. Сотрудник видит обмен своей страховой компании (ExchangeAdmin).

Откат снимает право с обеих групп.
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

GROUPS = ["Администратор справочников", "Сотрудник"]
CODENAME = "view_exchange"


def grant(apps, schema_editor):
    # На чистой базе права появляются только после всех миграций -
    # создаём их заранее (как в accounts/0003).
    app_config = apps.get_app_config("integrations")
    app_config.models_module = True
    create_permissions(app_config, apps=apps, verbosity=0)
    app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    permission = Permission.objects.get(content_type__app_label="integrations", codename=CODENAME)
    for group in Group.objects.filter(name__in=GROUPS):
        group.permissions.add(permission)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    permission = Permission.objects.filter(
        content_type__app_label="integrations", codename=CODENAME
    ).first()
    if permission is not None:
        for group in Group.objects.filter(name__in=GROUPS):
            group.permissions.remove(permission)


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0001_initial"),
        ("accounts", "0003_access_groups"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
