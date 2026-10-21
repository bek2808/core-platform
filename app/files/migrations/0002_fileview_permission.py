"""
Право «просмотр журнала просмотров файлов» - роли «Администратор страховой»
(files.view_fileview). Журнал только для чтения у всех: его пишет сама система
при открытии файла. Администратор страховой видит просмотры своей страховой
(files/admin.py); администратор платформы права не получает.

Таблиц не меняет. Откат снимает право с роли.
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

COMPANY_ADMIN_ROLE = "company_admin"
CODENAME = "view_fileview"


def grant(apps, schema_editor):
    # На чистой базе права появляются только после всех миграций -
    # создаём их заранее (как в accounts/0003).
    app_config = apps.get_app_config("files")
    app_config.models_module = True
    create_permissions(app_config, apps=apps, verbosity=0)
    app_config.models_module = None

    Role = apps.get_model("accounts", "Role")
    Permission = apps.get_model("auth", "Permission")
    permission = Permission.objects.get(content_type__app_label="files", codename=CODENAME)
    Role.objects.select_related("group").get(code=COMPANY_ADMIN_ROLE).group.permissions.add(permission)


def revoke(apps, schema_editor):
    # Прямыми SQL-командами: через модели Django откат путает версии моделей
    # прав и групп («Must be Group instance», см. agents/0005).
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM auth_group_permissions
            WHERE group_id = (SELECT group_id FROM accounts_role WHERE code = %s)
              AND permission_id IN (
                  SELECT p.id FROM auth_permission p JOIN django_content_type ct ON ct.id = p.content_type_id
                  WHERE ct.app_label = 'files' AND p.codename = %s)
            """,
            [COMPANY_ADMIN_ROLE, CODENAME],
        )


class Migration(migrations.Migration):

    dependencies = [
        ("files", "0001_initial"),
        ("accounts", "0009_role_visibility_agency"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
