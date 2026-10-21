"""
Проверка входа и прав Core-системы (модель: docs/users-model.md, «Прикрепление»).

Права человека страховой = права ролей его прикрепления, действующего
сегодня (по часовому поясу системы). Перевод сразу меняет права, история
ролей остаётся в прикреплениях. Нет действующего прикрепления (ещё не
начал, уже закрыто) - нет прав.

Вход (часть A3): человек страховой без действующего прикрепления не входит
вовсе - уволенный с будущей даты перестаёт входить на следующий день после
неё, принятый с будущей даты начинает входить с неё. Суперпользователь
и администратор платформы - без этого правила (прикреплений у них нет).

Выключенная страховая (снята отметка «работает», часть A4): её сотрудники
не входят, открытые сессии перестают действовать; данные остаются.

Пользователь агента (вид «агент», этап B3): входит, только пока у него
действующая связь с агентом (agents.Member), а у агента - действующее
соглашение. Соглашение закончилось - вход закрывается сам на следующий
день. Права - роль «Директор агента» или «Агент» по отметке «директор»
в связи (каталог ролей; права в роли придут вместе с договорами).

Группы в учётной записи действуют только у администратора платформы
(вид «платформа»): у него нет страховой и прикреплений.

Отдельные права в учётной записи (user_permissions) действуют как раньше:
их может задать только суперпользователь - аварийная мера, не способ работы.

Суперпользователь (аварийная техническая запись) по-прежнему имеет все права
без проверки - так устроен Django (PermissionsMixin.has_perm).
Заблокированный человек («активен» снят) прав не имеет и не входит.
"""
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import Permission

from .models import Assignment, User


class AssignmentBackend(ModelBackend):
    def user_can_authenticate(self, user):
        if not super().user_can_authenticate(user):  # «активен» снят
            return False
        if user.is_superuser or user.kind == User.Kind.PLATFORM:
            return True
        if user.insurance_company_id is not None and not user.insurance_company.is_active:
            return False
        if user.kind == User.Kind.AGENT:
            from agents.models import agent_user_may_work  # agents зависит от accounts

            return agent_user_may_work(user)
        return Assignment.objects.on().filter(user=user).exists()

    def _get_group_permissions(self, user_obj):
        if user_obj.kind == User.Kind.PLATFORM:
            return super()._get_group_permissions(user_obj)
        if user_obj.kind == User.Kind.AGENT:
            from agents.models import agent_user_may_work, current_membership

            if not agent_user_may_work(user_obj):
                return Permission.objects.none()
            role = current_membership(user_obj).role_code()
            return Permission.objects.filter(group__role__code=role).distinct()
        current = Assignment.objects.on().filter(user=user_obj)
        return Permission.objects.filter(group__role__assignments__in=current).distinct()
