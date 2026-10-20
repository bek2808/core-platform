"""
Стартовые списки оснований полномочий и должностей представителей.

Решение Бекзода 20.10.2026: заполнить сразу, а сверку с нормативными
документами Узбекистана (и с юристом) провести позже. Лишние пункты
администратор справочников выключит отметкой «действует», недостающие
добавит сам; эта миграция их не тронет.

Пункт «Иное» в каждом списке - последний и единственный: при его выборе
значение пишется текстом.
"""
from django.db import migrations

POWER_BASES = [
    "Устав",
    "Положение",
    "Доверенность",
    "Приказ",
    "Решение",
    "Свидетельство о государственной регистрации",
    "Закон или указ",
    "Договор",
]

POSITIONS = [
    "Директор",
    "Генеральный директор",
    "Председатель правления",
    "Исполнительный директор",
    "Заместитель директора",
    "Заместитель председателя правления",
    "Председатель",
    "Начальник",
    "Управляющий",
    "Руководитель филиала",
    "Главный бухгалтер",
]

OTHER = "Иное"
OTHER_SORT_ORDER = 999


def fill(model, names):
    for number, name in enumerate(names, start=1):
        model.objects.get_or_create(name=name, defaults={"sort_order": number * 10})
    model.objects.get_or_create(
        name=OTHER, defaults={"is_other": True, "sort_order": OTHER_SORT_ORDER}
    )


def add_lists(apps, schema_editor):
    fill(apps.get_model("counterparties", "PowerBasis"), POWER_BASES)
    fill(apps.get_model("counterparties", "Position"), POSITIONS)


def remove_lists(apps, schema_editor):
    # Откат: убираем только то, что добавила эта миграция.
    apps.get_model("counterparties", "PowerBasis").objects.filter(
        name__in=[*POWER_BASES, OTHER]
    ).delete()
    apps.get_model("counterparties", "Position").objects.filter(
        name__in=[*POSITIONS, OTHER]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("counterparties", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(add_lists, remove_lists),
    ]
