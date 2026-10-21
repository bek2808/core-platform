"""
Роль «Работа с агентами» (код agents) и права на агентов (часть B2).

- Новая роль сотрудников страховой «Работа с агентами»: агенты,
  соглашения, места агентов, связь агента с ЕРСП - смотреть, заводить,
  менять (прекратить соглашение, перевести); удаления нет. Для выбора
  контрагента и счёта - просмотр контрагентов, данных юрлица и счетов;
  справочник типов местности и справочник агентов ЕРСП - просмотр;
  журнал изменений - просмотр (кнопка «История»). В Mosaic её получают
  директоры отделов (решение 27) - назначает администратор страховой.
- Роль «Администратор страховой» получает те же права на агентов
  (агенты входят во «всё своей страховой»).

Администратор платформы этих прав не получает. Таблиц не меняет.
Откат снимает права с администратора страховой и удаляет роль и её группу.
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

ROLE_CODE = "agents"
GROUP_NAME = "Работа с агентами"
COMPANY_ADMIN_ROLE = "company_admin"
DESCRIPTION = (
    "Агенты и брокеры своей страховой: заводит агентов, соглашения, прикрепляет к подразделениям "
    "и переводит, прекращает соглашения, связывает агентов с ЕРСП (agencyId)."
)
AGENT_RIGHTS = [
    ("agents", f"{action}_{model}")
    for model in ("agent", "agreement", "placement", "ersplink")
    for action in ("view", "add", "change")
]
ROLE_EXTRA = [
    ("counterparties", "view_counterparty"),
    ("counterparties", "view_legalentity"),
    ("counterparties", "view_bankaccount"),
    ("references", "view_erspareatype"),
    ("integrations", "view_erspagency"),
    ("audit", "view_changelog"),
]


def find(apps, wanted, create=True):
    # На чистой базе права появляются только после всех миграций -
    # создаём их заранее (как в accounts/0003). При откате права уже есть:
    # только находим их.
    if create:
        for label in sorted({app_label for app_label, _ in wanted}):
            app_config = apps.get_app_config(label)
            app_config.models_module = True
            create_permissions(app_config, apps=apps, verbosity=0)
            app_config.models_module = None
    Permission = apps.get_model("auth", "Permission")
    found = []
    for app_label, codename in wanted:
        permission = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
        if permission is None:
            if not create:
                continue
            raise RuntimeError(f"Нет права {app_label}.{codename} - роль «{GROUP_NAME}» не заведена.")
        found.append(permission)
    return found


def add_role(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Role = apps.get_model("accounts", "Role")
    group, _ = Group.objects.get_or_create(name=GROUP_NAME)
    group.permissions.set(find(apps, AGENT_RIGHTS + ROLE_EXTRA))
    Role.objects.update_or_create(
        code=ROLE_CODE,
        defaults={"group": group, "audience": "company", "default_visibility": "department",
                  "description": DESCRIPTION, "sort_order": 60},
    )
    Role.objects.select_related("group").get(code=COMPANY_ADMIN_ROLE).group.permissions.add(
        *find(apps, AGENT_RIGHTS)
    )


def remove_role(apps, schema_editor):
    # Откат - прямыми SQL-командами по номерам записей: через модели Django
    # при откате путает версии моделей прав и групп («Must be Group instance»).
    # Права на связи с ЕРСП администратор страховой получил раньше (agents/0002) - их не трогаем.
    codenames = [codename for _, codename in AGENT_RIGHTS if not codename.endswith("_ersplink")]
    with schema_editor.connection.cursor() as cursor:
        # Права на агентов - снять с администратора страховой.
        cursor.execute(
            """
            DELETE FROM auth_group_permissions
            WHERE group_id = (SELECT group_id FROM accounts_role WHERE code = %s)
              AND permission_id IN (
                  SELECT p.id FROM auth_permission p JOIN django_content_type ct ON ct.id = p.content_type_id
                  WHERE ct.app_label = 'agents' AND p.codename = ANY(%s))
            """,
            [COMPANY_ADMIN_ROLE, codenames],
        )
        # Роль «Работа с агентами»: из прикреплений, сама роль, права группы, группа.
        cursor.execute("SELECT id, group_id FROM accounts_role WHERE code = %s", [ROLE_CODE])
        row = cursor.fetchone()
        if row is None:
            return
        role_id, group_id = row
        cursor.execute("DELETE FROM accounts_assignment_roles WHERE role_id = %s", [role_id])
        cursor.execute("DELETE FROM accounts_role WHERE id = %s", [role_id])
        cursor.execute("DELETE FROM auth_group_permissions WHERE group_id = %s", [group_id])
        cursor.execute("DELETE FROM accounts_user_groups WHERE group_id = %s", [group_id])
        cursor.execute("DELETE FROM auth_group WHERE id = %s", [group_id])


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0004_agent_rules"),
        ("accounts", "0008_platform_admin"),
        ("references", "0006_new_references_view_permission"),
        ("counterparties", "0006_representative_form_labels"),
        ("audit", "0003_changelog_source_cbu"),
    ]

    operations = [
        migrations.RunPython(add_role, remove_role),
    ]
