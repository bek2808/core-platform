"""
Правила агентов, которые смотрят на другие строки, - триггерами в базе
(модель: docs/users-model.md, «Агенты (этап B)»). Те же правила - в clean()
моделей; триггер держит их, даже если записать в обход экрана.

- agents_agent_rules: страховая и контрагент агента не меняются после
  создания; контрагент, головной электронный агент и счёт для комиссии -
  своей страховой (счёт - именно этого контрагента); «входит в
  электронного» - только головной электронный агент;
- agents_agreement_rules: агент соглашения не меняется; страховая -
  как у агента; подписант (если выбран пользователь) - своей страховой;
- agents_placement_rules: агент места не меняется; подразделение -
  своей страховой;
- agents_ersplink_rules (заменяет триггер из agents/0001): к прежним
  правилам связи добавлен агент - своей страховой, не меняется.

Таблиц и данных не меняет. Откат удаляет новые триггеры и возвращает
прежний триггер связи (только для подразделений).
"""
from django.db import migrations

RULES_SQL = """
CREATE FUNCTION agents_agent_rules() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    row_company bigint;
    head_is_head boolean;
BEGIN
    IF TG_OP = 'UPDATE' AND (
        NEW.insurance_company_id <> OLD.insurance_company_id
        OR NEW.counterparty_id <> OLD.counterparty_id
    ) THEN
        RAISE EXCEPTION 'Агент %: страховая и контрагент не меняются после создания', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT insurance_company_id INTO row_company FROM counterparties_counterparty WHERE id = NEW.counterparty_id;
    IF row_company <> NEW.insurance_company_id THEN
        RAISE EXCEPTION 'Агент: контрагент другой страховой' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.head_agent_id IS NOT NULL THEN
        SELECT insurance_company_id, is_head_electronic INTO row_company, head_is_head
            FROM agents_agent WHERE id = NEW.head_agent_id;
        IF row_company <> NEW.insurance_company_id OR NOT head_is_head THEN
            RAISE EXCEPTION 'Агент: «входит в электронного» - только головной электронный агент своей страховой'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF NEW.commission_account_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM counterparties_bankaccount
            WHERE id = NEW.commission_account_id AND counterparty_id = NEW.counterparty_id
    ) THEN
        RAISE EXCEPTION 'Агент: счёт для комиссии - не счёт этого контрагента' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER agents_agent_rules
    BEFORE INSERT OR UPDATE ON agents_agent
    FOR EACH ROW EXECUTE FUNCTION agents_agent_rules();

CREATE FUNCTION agents_agreement_rules() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    row_company bigint;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.agent_id <> OLD.agent_id THEN
        RAISE EXCEPTION 'Соглашение %: агент не меняется', OLD.id USING ERRCODE = 'check_violation';
    END IF;
    SELECT insurance_company_id INTO row_company FROM agents_agent WHERE id = NEW.agent_id;
    IF row_company <> NEW.insurance_company_id THEN
        RAISE EXCEPTION 'Соглашение: страховая не совпадает со страховой агента' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.signed_by_id IS NOT NULL THEN
        SELECT insurance_company_id INTO row_company FROM accounts_user WHERE id = NEW.signed_by_id;
        IF row_company IS DISTINCT FROM NEW.insurance_company_id THEN
            RAISE EXCEPTION 'Соглашение: подписант из другой страховой' USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER agents_agreement_rules
    BEFORE INSERT OR UPDATE ON agents_agreement
    FOR EACH ROW EXECUTE FUNCTION agents_agreement_rules();

CREATE FUNCTION agents_placement_rules() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    agent_company bigint;
    department_company bigint;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.agent_id <> OLD.agent_id THEN
        RAISE EXCEPTION 'Место агента %: агент не меняется', OLD.id USING ERRCODE = 'check_violation';
    END IF;
    SELECT insurance_company_id INTO agent_company FROM agents_agent WHERE id = NEW.agent_id;
    SELECT insurance_company_id INTO department_company FROM companies_department WHERE id = NEW.department_id;
    IF agent_company <> department_company THEN
        RAISE EXCEPTION 'Место агента: подразделение другой страховой' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER agents_placement_rules
    BEFORE INSERT OR UPDATE ON agents_placement
    FOR EACH ROW EXECUTE FUNCTION agents_placement_rules();

CREATE OR REPLACE FUNCTION agents_ersplink_rules() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    agency_company bigint;
    agency_environment varchar;
    department_company bigint;
    department_kind varchar;
    agent_company bigint;
BEGIN
    IF TG_OP = 'UPDATE' AND (
        NEW.ersp_agency_id <> OLD.ersp_agency_id
        OR NEW.department_id IS DISTINCT FROM OLD.department_id
        OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
        OR NEW.insurance_company_id <> OLD.insurance_company_id
        OR NEW.environment <> OLD.environment
    ) THEN
        RAISE EXCEPTION 'Связь с ЕРСП %: запись ЕРСП, подразделение, агент, страховая и среда не меняются', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT insurance_company_id, environment INTO agency_company, agency_environment
        FROM integrations_erspagency WHERE id = NEW.ersp_agency_id;
    IF agency_company <> NEW.insurance_company_id OR agency_environment <> NEW.environment THEN
        RAISE EXCEPTION 'Связь с ЕРСП: страховая и среда должны совпадать с записью ЕРСП'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.department_id IS NOT NULL THEN
        SELECT insurance_company_id, kind INTO department_company, department_kind
            FROM companies_department WHERE id = NEW.department_id;
        IF department_company <> NEW.insurance_company_id THEN
            RAISE EXCEPTION 'Связь с ЕРСП: подразделение другой страховой'
                USING ERRCODE = 'check_violation';
        END IF;
        IF department_kind = 'division' THEN
            RAISE EXCEPTION 'Связь с ЕРСП: отдел не связывается (в ЕРСП отделов нет)'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF NEW.agent_id IS NOT NULL THEN
        SELECT insurance_company_id INTO agent_company FROM agents_agent WHERE id = NEW.agent_id;
        IF agent_company <> NEW.insurance_company_id THEN
            RAISE EXCEPTION 'Связь с ЕРСП: агент другой страховой' USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$$;
"""

REVERSE_SQL = """
DROP TRIGGER agents_agent_rules ON agents_agent;
DROP FUNCTION agents_agent_rules();
DROP TRIGGER agents_agreement_rules ON agents_agreement;
DROP FUNCTION agents_agreement_rules();
DROP TRIGGER agents_placement_rules ON agents_placement;
DROP FUNCTION agents_placement_rules();

CREATE OR REPLACE FUNCTION agents_ersplink_rules() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    agency_company bigint;
    agency_environment varchar;
    department_company bigint;
    department_kind varchar;
BEGIN
    IF TG_OP = 'UPDATE' AND (
        NEW.ersp_agency_id <> OLD.ersp_agency_id
        OR NEW.department_id IS DISTINCT FROM OLD.department_id
        OR NEW.insurance_company_id <> OLD.insurance_company_id
        OR NEW.environment <> OLD.environment
    ) THEN
        RAISE EXCEPTION 'Связь с ЕРСП %: запись ЕРСП, подразделение, страховая и среда не меняются', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT insurance_company_id, environment INTO agency_company, agency_environment
        FROM integrations_erspagency WHERE id = NEW.ersp_agency_id;
    IF agency_company <> NEW.insurance_company_id OR agency_environment <> NEW.environment THEN
        RAISE EXCEPTION 'Связь с ЕРСП: страховая и среда должны совпадать с записью ЕРСП'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.department_id IS NOT NULL THEN
        SELECT insurance_company_id, kind INTO department_company, department_kind
            FROM companies_department WHERE id = NEW.department_id;
        IF department_company <> NEW.insurance_company_id THEN
            RAISE EXCEPTION 'Связь с ЕРСП: подразделение другой страховой'
                USING ERRCODE = 'check_violation';
        END IF;
        IF department_kind = 'division' THEN
            RAISE EXCEPTION 'Связь с ЕРСП: отдел не связывается (в ЕРСП отделов нет)'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$$;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0003_agents_agreements_placements"),
    ]

    operations = [
        migrations.RunSQL(sql=RULES_SQL, reverse_sql=REVERSE_SQL),
    ]
