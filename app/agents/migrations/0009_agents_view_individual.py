"""
Роль «Работа с агентами» смотрит данные физлица: агентом может быть физлицо,
и при выборе контрагента нужны его данные (counterparties.Individual).
Таблиц не меняет. Откат снимает право (прямым SQL).
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

ROLE_CODE = "agents"


def give(apps, schema_editor):
    app_config = apps.get_app_config("counterparties")
    app_config.models_module = True
    create_permissions(app_config, apps=apps, verbosity=0)
    app_config.models_module = None
    Role = apps.get_model("accounts", "Role")
    Permission = apps.get_model("auth", "Permission")
    group = Role.objects.select_related("group").get(code=ROLE_CODE).group
    group.permissions.add(Permission.objects.get(
        content_type__app_label="counterparties", codename="view_individual",
    ))


def take_back(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM auth_group_permissions
            WHERE group_id = (SELECT group_id FROM accounts_role WHERE code = %s)
              AND permission_id IN (
                  SELECT p.id FROM auth_permission p JOIN django_content_type ct ON ct.id = p.content_type_id
                  WHERE ct.app_label = 'counterparties' AND p.codename = 'view_individual')
            """,
            [ROLE_CODE],
        )


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0008_agreement_scan"),
        ("counterparties", "0008_individual_permissions"),
    ]

    operations = [
        migrations.RunPython(give, take_back),
    ]
