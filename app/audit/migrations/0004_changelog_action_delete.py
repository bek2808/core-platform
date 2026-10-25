"""
Журнал изменений: новое действие «удаление» (delete).

Меняется только список допустимых значений поля action в Django; таблица
и данные в базе не меняются (choices в базе не хранятся).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0003_changelog_source_cbu"),
    ]

    operations = [
        migrations.AlterField(
            model_name="changelog",
            name="action",
            field=models.CharField(
                choices=[("create", "создание"), ("update", "изменение"), ("delete", "удаление")],
                max_length=16,
                verbose_name="действие",
            ),
        ),
    ]
