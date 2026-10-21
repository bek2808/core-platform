"""
Журнал изменений у страховых компаний (часть A4, docs/users-model.md).

Карточка страховой переходит на основу TrackedModel: поля «создал»
и «изменил» плюс журнал «было -> стало» (кто завёл страховую, сменил ей
запись в ЕРСП, выключил). У нынешних страховых «создал» и «изменил» - пусто
(система): кто их заводил раньше, не записывалось.

Отметка «работает» получает пояснение: снятая закрывает вход сотрудникам
(accounts/backends.py). Таблица в базе не меняется, кроме двух новых
столбцов. Откат: столбцы удаляются.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("companies", "0003_department_access"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="insurancecompany",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                help_text="Пусто - запись создана системой (загрузка, миграция).",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="создал",
            ),
        ),
        migrations.AddField(
            model_name="insurancecompany",
            name="updated_by",
            field=models.ForeignKey(
                blank=True,
                help_text="Пусто - последнее изменение сделала система.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="изменил",
            ),
        ),
        migrations.AlterField(
            model_name="insurancecompany",
            name="is_active",
            field=models.BooleanField(
                default=True,
                help_text="Снята - сотрудники страховой не входят в систему; данные остаются.",
                verbose_name="работает",
            ),
        ),
    ]
