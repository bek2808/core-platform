"""
Права - из ролей прикрепления: перенос существующих пользователей (часть A2.3).

С этой версии права человека страховой - роли его действующего прикрепления
(accounts/backends.py), группы в учётной записи у него не действуют.
Чтобы никто не потерял права, миграция:

1. Каждому пользователю страховой (не платформа), у которого нет
   прикрепления, заводит прикрепление: головной офис его страховой,
   должность «Не указана», с даты регистрации, роли - по его нынешним
   группам, основание - «перенос прав (A2.3)». Тем, у кого прикрепление уже
   есть (bekzod завёл своё через экран 21.10), ничего не заводится.
2. Тем, у кого прикрепление уже есть, нынешние группы добавляются к ролям
   действующего прикрепления - права не сужаются.
3. Очищает группы в учётной записи у пользователей страховой: они больше
   не действуют, и экран их не показывает. У платформы группы остаются.
4. «Статус персонала» выставляет по правилу системы: да у всех, кроме агентов.

Суперпользователь имеет все права без ролей - этот перенос его доступ
не затрагивает. Записи идут от имени системы, в журнал не пишутся.

Откат: группы возвращаются по ролям действующих прикреплений, прикрепления
с основанием «перенос прав (A2.3)» удаляются. Перед применением - резервная копия.
"""
from django.db import migrations
from django.utils import timezone

MIGRATION_BASIS = "перенос прав (A2.3)"
NOT_SPECIFIED = "Не указана"


def current(Assignment, user_id, today):
    return (
        Assignment.objects.filter(user_id=user_id, valid_from__lte=today)
        .exclude(valid_to__lt=today)
        .first()
    )


def move_rights(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Assignment = apps.get_model("accounts", "Assignment")
    JobPosition = apps.get_model("accounts", "JobPosition")
    Role = apps.get_model("accounts", "Role")
    Department = apps.get_model("companies", "Department")
    today = timezone.localdate()
    not_specified = JobPosition.objects.get(name=NOT_SPECIFIED)

    for user in User._base_manager.filter(insurance_company__isnull=False).exclude(kind="platform"):
        roles = list(Role.objects.filter(group__in=user.groups.all()))
        if not Assignment.objects.filter(user_id=user.pk).exists():
            head_office = Department.objects.get(insurance_company_id=user.insurance_company_id, kind="head_office")
            joined = timezone.localtime(user.date_joined).date() if user.date_joined else today
            assignment = Assignment.objects.create(
                user_id=user.pk, department=head_office, position=not_specified,
                valid_from=min(joined, today), basis=MIGRATION_BASIS,
            )
            assignment.roles.set(roles)
        elif roles:
            assignment = current(Assignment, user.pk, today)
            if assignment is not None:
                assignment.roles.add(*roles)
        user.groups.clear()

    # «Статус персонала» - по правилу системы (accounts.models.User.normalize).
    User._base_manager.exclude(kind="agent").update(is_staff=True)
    User._base_manager.filter(kind="agent").update(is_staff=False)


def return_groups(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Assignment = apps.get_model("accounts", "Assignment")
    today = timezone.localdate()
    for user in User._base_manager.filter(insurance_company__isnull=False).exclude(kind="platform"):
        assignment = current(Assignment, user.pk, today)
        if assignment is not None:
            user.groups.add(*[role.group for role in assignment.roles.select_related("group")])
    for assignment in Assignment.objects.filter(basis=MIGRATION_BASIS):
        assignment.roles.clear()
    # Прикрепления, заведённые этой миграцией. _base_manager: обычный
    # менеджер запрещает удаление (журнал), а откат - от имени системы.
    Assignment._base_manager.filter(basis=MIGRATION_BASIS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_roles_positions_assignments"),
        ("companies", "0003_department_access"),
    ]

    operations = [
        migrations.RunPython(move_rights, return_groups),
    ]
