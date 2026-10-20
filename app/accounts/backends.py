"""
Проверка прав Core-системы (модель: docs/users-model.md, «Прикрепление»).

Права человека страховой = права ролей его прикрепления, действующего
сегодня (по часовому поясу системы). Перевод сразу меняет права, история
ролей остаётся в прикреплениях. Нет действующего прикрепления (ещё не
начал, уже закрыто) - нет прав.

Группы в учётной записи действуют только у администратора платформы
(вид «платформа»): у него нет страховой и прикреплений.

Отдельные права в учётной записи (user_permissions) действуют как раньше:
их может задать только суперпользователь - аварийная мера, не способ работы.

Суперпользователь (аварийная техническая запись) по-прежнему имеет все права
без проверки - так устроен Django (PermissionsMixin.has_perm).
Заблокированный человек («активен» снят) прав не имеет.
"""
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import Permission

from .models import Assignment, User


class AssignmentBackend(ModelBackend):
    def _get_group_permissions(self, user_obj):
        if user_obj.kind == User.Kind.PLATFORM:
            return super()._get_group_permissions(user_obj)
        current = Assignment.objects.on().filter(user=user_obj)
        return Permission.objects.filter(group__role__assignments__in=current).distinct()
