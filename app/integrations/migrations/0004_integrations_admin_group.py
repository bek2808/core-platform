"""
Группа «Администратор интеграций» (решение Бекзода 20.10).

Права группы:
- карточка подключения к ЕРСП своей страховой: просмотр и правка
  (данные для входа, проверка подключения, среда отправки полисов);
- журнал обмена своей страховой - просмотр;
- журнал изменений - просмотр (кнопка «История» в карточке).
Контрагентов и справочников в группе нет: если нужны, человеку дают
ещё и «Администратора справочников».

Чтобы человек вошёл в админ-панель, кроме группы ему нужна отметка
«статус персонала» (is_staff) и страховая компания в учётной записи.

Миграция не меняет таблиц. Откат удаляет группу (участники теряют эти права).
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

GROUP = "Администратор интеграций"
PERMISSIONS = [
    ("integrations", "view_erspconnection"),
    ("integrations", "change_erspconnection"),
    ("integrations", "view_erspcredentials"),
    ("integrations", "change_erspcredentials"),
    ("integrations", "view_exchange"),
    ("audit", "view_changelog"),
]


def add_group(apps, schema_editor):
    # На чистой базе права появляются только после всех миграций -
    # создаём их заранее (как в accounts/0003).
    for label in ("integrations", "audit"):
        app_config = apps.get_app_config(label)
        app_config.models_module = True
        create_permissions(app_config, apps=apps, verbosity=0)
        app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    group, _ = Group.objects.get_or_create(name=GROUP)
    permissions = []
    for app_label, codename in PERMISSIONS:
        permission = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
        if permission is None:
            raise RuntimeError(f"Нет права {app_label}.{codename} - группа «{GROUP}» не создана.")
        permissions.append(permission)
    group.permissions.set(permissions)


def remove_group(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name=GROUP).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0003_ersp_connection"),
        ("audit", "0002_changelog_card"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(add_group, remove_group),
    ]
