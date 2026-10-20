"""
ОКЭД и валюта капитала - из справочников ЕРСП (решение Бекзода 20.10.2026).

Что делает миграция с данными юрлиц:
- ОКЭД: текстовый код (65120) становится ссылкой на справочник ОКЭД ЕРСП;
- валюта капитала: текстовый код (UZS) становится ссылкой на справочник
  валют ЕРСП (сум - id 860);
- поле «вид деятельности» удаляется: название берётся из справочника ОКЭД,
  а ответ провайдера целиком будет храниться в модуле интеграции.

ПОРЯДОК НА СЕРВЕРЕ: сначала справочники должны быть загружены:
    migrate references
    load_ersp_references
    migrate counterparties
Если код ОКЭД или валюта не найдены в справочнике, миграция останавливается
с понятной ошибкой, и в базе не меняется ничего (вся миграция - одна
транзакция).

Откат (migrate counterparties 0003): ОКЭД и валюта возвращаются текстом
из справочника; «вид деятельности» возвращается ПУСТЫМ - его прежнее
значение есть только в резервной копии, сделанной перед миграцией.
Изменения этой миграции в журнал изменений не пишутся (миграция работает
напрямую с таблицей).
"""
import django.db.models.deletion
from django.db import migrations, models

UZS = 860


def to_references(apps, schema_editor):
    LegalEntity = apps.get_model("counterparties", "LegalEntity")
    ErspOked = apps.get_model("references", "ErspOked")
    ErspCurrency = apps.get_model("references", "ErspCurrency")

    rows = list(LegalEntity.objects.all())
    if not rows:
        return
    # Буквенные коды в ЕРСП местами с пробелами («TJS »): сравниваем без них.
    currency_by_ticker = {c.ticker.strip().upper(): c.pk for c in ErspCurrency.objects.all()}
    problems = []
    for legal in rows:
        if legal.oked_code:
            if ErspOked.objects.filter(pk=legal.oked_code).exists():
                legal.oked_id = legal.oked_code
            else:
                problems.append(f"ИНН {legal.inn}: ОКЭД {legal.oked_code} нет в справочнике ЕРСП")
        if legal.charter_capital is not None:
            code = (legal.charter_capital_currency_code or "UZS").strip().upper()
            currency_id = currency_by_ticker.get(code)
            if currency_id is None:
                problems.append(f"ИНН {legal.inn}: валюты {code} нет в справочнике ЕРСП")
            else:
                legal.charter_capital_currency_id = currency_id
        legal.save(update_fields=["oked", "charter_capital_currency"])
    if problems:
        raise RuntimeError(
            "Миграция остановлена, в базе ничего не изменилось. "
            + "; ".join(problems)
            + ". Сначала загрузите справочники ЕРСП: "
            "bin/core-run podman exec core-web python manage.py load_ersp_references"
        )
    # Проверить ссылки сейчас, а не в конце транзакции: дальше в этой же
    # миграции удаляются столбцы, а PostgreSQL не меняет таблицу, пока по ней
    # есть отложенные проверки.
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def from_references(apps, schema_editor):
    LegalEntity = apps.get_model("counterparties", "LegalEntity")
    for legal in LegalEntity.objects.select_related("charter_capital_currency"):
        legal.oked_code = legal.oked_id or ""
        currency = legal.charter_capital_currency
        legal.charter_capital_currency_code = currency.ticker.strip() if currency else "UZS"
        legal.save(update_fields=["oked_code", "charter_capital_currency_code"])
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class Migration(migrations.Migration):

    dependencies = [
        ("counterparties", "0003_counterparty_legal_entity"),
        ("references", "0002_ersp_districts_currencies_okeds"),
    ]

    operations = [
        # 1. Старые проверки формата текстовых полей больше не нужны.
        migrations.RemoveConstraint(model_name="legalentity", name="counterparties_legalentity_oked"),
        migrations.RemoveConstraint(model_name="legalentity", name="counterparties_legalentity_currency"),
        # 2. Старые текстовые поля временно переименовываются...
        migrations.RenameField(model_name="legalentity", old_name="oked", new_name="oked_code"),
        migrations.RenameField(
            model_name="legalentity",
            old_name="charter_capital_currency",
            new_name="charter_capital_currency_code",
        ),
        # 3. ...рядом появляются ссылки на справочники ЕРСП...
        migrations.AddField(
            model_name="legalentity",
            name="oked",
            field=models.ForeignKey(
                blank=True,
                help_text="Из справочника ЕРСП; название показывается из него.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="references.erspoked",
                verbose_name="ОКЭД",
            ),
        ),
        migrations.AddField(
            model_name="legalentity",
            name="charter_capital_currency",
            field=models.ForeignKey(
                blank=True,
                help_text="Из справочника ЕРСП. Если капитал указан, а валюта нет - сум (провайдер отдаёт капитал в сумах).",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="references.erspcurrency",
                verbose_name="валюта капитала",
            ),
        ),
        # 4. ...данные переносятся из текста в ссылки...
        migrations.RunPython(to_references, from_references),
        # 5. ...и старые текстовые поля удаляются.
        migrations.RemoveField(model_name="legalentity", name="oked_code"),
        migrations.RemoveField(model_name="legalentity", name="charter_capital_currency_code"),
        migrations.RemoveField(model_name="legalentity", name="oked_title"),
        # Подпись «Это не страховая лицензия» убрана (в базе ничего не меняется).
        migrations.AlterField(
            model_name="legalentity",
            name="reg_certificate_number",
            field=models.CharField(
                blank=True, max_length=50, verbose_name="номер свидетельства о регистрации"
            ),
        ),
        migrations.AddConstraint(
            model_name="legalentity",
            constraint=models.CheckConstraint(
                condition=models.Q(("charter_capital__isnull", True), ("charter_capital_currency__isnull", False), _connector="OR"),
                name="counterparties_legalentity_capital_has_currency",
                violation_error_message="У уставного капитала должна быть валюта.",
            ),
        ),
    ]
