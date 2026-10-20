"""
Помощники автотестов: дать сотруднику роли так, как это делается в системе.

С части A2.3 права человека страховой - это роли его действующего
прикрепления, а не группы в учётной записи. Тесты других приложений
дают права этим помощником, а не user.groups.add(...).
"""
import datetime

from django.contrib.auth.models import Group

from .models import Assignment, JobPosition, Role, User

# Прикрепление в тестах действует давно и без конца - на любой «сегодня».
LONG_AGO = datetime.date(2000, 1, 1)


def give_roles(user, *group_names, department=None):
    """
    Дать пользователю роли (по названию группы) прикреплением, действующим сегодня.

    Если действующее прикрепление уже есть - роли добавляются к нему.
    Пользователь без страховой - администратор платформы: ему группы
    даются в учётную запись (у платформы прикреплений нет).
    """
    groups = [Group.objects.get(name=name) for name in group_names]
    if user.insurance_company_id is None:
        if user.kind != User.Kind.PLATFORM:
            user.kind = User.Kind.PLATFORM
            user.save(update_fields=["kind"])
        user.groups.add(*groups)
        return None
    assignment = Assignment.objects.on().filter(user=user).first()
    if assignment is None:
        from companies.models import Department

        assignment = Assignment(
            user=user,
            department=department or Department.head_office_for(user.insurance_company),
            position=JobPosition.objects.get(name="Не указана"),
            valid_from=LONG_AGO,
        )
        assignment.save()
    roles = Role.objects.filter(group__in=groups)
    if len(roles) != len(groups):
        raise ValueError(f"У групп {group_names} нет ролей - проверьте названия.")
    assignment.set_roles([*assignment.roles.all(), *roles])
    return assignment
