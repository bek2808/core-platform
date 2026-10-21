"""
Пользователи агента: правила в базе, роли и права (часть B3).

1. Триггер agents_member_rules: пользователь - вида «агент» и из страховой
   агента; пользователь и агент связи не меняются после создания.
2. Роли «Агент» (код agent, видимость «свои») и «Директор агента»
   (код agent_director, видимость «агентство»), для кого - агенты.
   Права в них придут вместе с договорами; у пользователя агента роль
   задаёт отметка «директор» (accounts/backends.py).
3. Права на пользователей агента (смотреть, заводить, менять: «директор»,
   «Отключить», «Сбросить пароль») - ролям «Работа с агентами»
   и «Администратор страховой». Удаления нет.

Данные не меняются. Откат - прямыми SQL-командами по номерам записей
(через модели Django откат прав падает, AGENTS.md): удаляет роли агента
с их группами, снимает права на пользователей агента, удаляет триггер.
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

RULES_SQL = """
CREATE FUNCTION agents_member_rules() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    user_company bigint;
    user_kind varchar;
    agent_company bigint;
BEGIN
    IF TG_OP = 'UPDATE' AND (NEW.user_id <> OLD.user_id OR NEW.agent_id <> OLD.agent_id) THEN
        RAISE EXCEPTION 'Пользователь агента %: пользователь и агент не меняются', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT insurance_company_id, kind INTO user_company, user_kind FROM accounts_user WHERE id = NEW.user_id;
    SELECT insurance_company_id INTO agent_company FROM agents_agent WHERE id = NEW.agent_id;
    IF user_kind <> 'agent' THEN
        RAISE EXCEPTION 'Пользователь агента - только учётная запись вида «агент»' USING ERRCODE = 'check_violation';
    END IF;
    IF user_company IS DISTINCT FROM agent_company THEN
        RAISE EXCEPTION 'Пользователь агента - из другой страховой' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER agents_member_rules
    BEFORE INSERT OR UPDATE ON agents_member
    FOR EACH ROW EXECUTE FUNCTION agents_member_rules();
"""

REVERSE_RULES_SQL = """
DROP TRIGGER agents_member_rules ON agents_member;
DROP FUNCTION agents_member_rules();
"""

AGENT_ROLES = [
    ("agent", "Агент", "own", 70, "Пользователь агента: продажи разрешённых продуктов, видит свои продажи."),
    ("agent_director", "Директор агента", "agency", 80,
     "Директор агента: как «Агент», плюс видит продажи всего агентства."),
]
MEMBER_RIGHTS = [("agents", f"{action}_member") for action in ("view", "add", "change")]
GRANTED_TO = ["agents", "company_admin"]  # коды ролей


def forward(apps, schema_editor):
    app_config = apps.get_app_config("agents")
    app_config.models_module = True
    create_permissions(app_config, apps=apps, verbosity=0)
    app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    Role = apps.get_model("accounts", "Role")
    for code, name, visibility, order, description in AGENT_ROLES:
        group, _ = Group.objects.get_or_create(name=name)
        Role.objects.update_or_create(code=code, defaults={
            "group": group, "audience": "agent", "default_visibility": visibility,
            "description": description, "sort_order": order,
        })
    rights = [Permission.objects.get(content_type__app_label=app, codename=codename) for app, codename in MEMBER_RIGHTS]
    for role in Role.objects.select_related("group").filter(code__in=GRANTED_TO):
        role.group.permissions.add(*rights)


def backward(apps, schema_editor):
    codenames = [codename for _, codename in MEMBER_RIGHTS]
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM auth_group_permissions
            WHERE permission_id IN (
                SELECT p.id FROM auth_permission p JOIN django_content_type ct ON ct.id = p.content_type_id
                WHERE ct.app_label = 'agents' AND p.codename = ANY(%s))
            """,
            [codenames],
        )
        for code, *_ in AGENT_ROLES:
            cursor.execute("SELECT id, group_id FROM accounts_role WHERE code = %s", [code])
            row = cursor.fetchone()
            if row is None:
                continue
            role_id, group_id = row
            cursor.execute("DELETE FROM accounts_assignment_roles WHERE role_id = %s", [role_id])
            cursor.execute("DELETE FROM accounts_role WHERE id = %s", [role_id])
            cursor.execute("DELETE FROM auth_group_permissions WHERE group_id = %s", [group_id])
            cursor.execute("DELETE FROM accounts_user_groups WHERE group_id = %s", [group_id])
            cursor.execute("DELETE FROM auth_group WHERE id = %s", [group_id])


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0006_agent_members"),
        ("agents", "0005_agents_role"),
        ("accounts", "0009_role_visibility_agency"),
    ]

    operations = [
        migrations.RunSQL(sql=RULES_SQL, reverse_sql=REVERSE_RULES_SQL),
        migrations.RunPython(forward, backward),
    ]
