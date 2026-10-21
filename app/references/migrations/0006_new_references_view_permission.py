"""
Право «просмотр» четырёх новых справочников ЕРСП (типы местности, типы
обязательного страхования, типы резидентства, страны) - группам
«Администратор справочников» и «Сотрудник», как у прежних справочников ЕРСП
(accounts/0003). Править их не может никто: они меняются только загрузкой.

Таблиц не меняет. Откат снимает права с обеих групп.
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

GROUPS = ["Администратор справочников", "Сотрудник"]
CODENAMES = ["view_erspareatype", "view_erspinsurancetype", "view_erspresidenttype", "view_erspcountry"]


def grant(apps, schema_editor):
    # На чистой базе права появляются только после всех миграций -
    # создаём их заранее (как в accounts/0003).
    app_config = apps.get_app_config("references")
    app_config.models_module = True
    create_permissions(app_config, apps=apps, verbosity=0)
    app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    permissions = list(Permission.objects.filter(content_type__app_label="references", codename__in=CODENAMES))
    if len(permissions) != len(CODENAMES):
        raise RuntimeError("Не нашлись права на новые справочники ЕРСП - миграция остановлена.")
    for group in Group.objects.filter(name__in=GROUPS):
        group.permissions.add(*permissions)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    permissions = Permission.objects.filter(content_type__app_label="references", codename__in=CODENAMES)
    for group in Group.objects.filter(name__in=GROUPS):
        group.permissions.remove(*permissions)


class Migration(migrations.Migration):

    dependencies = [
        ("references", "0005_ersp_area_insurance_resident_countries"),
        ("accounts", "0003_access_groups"),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
