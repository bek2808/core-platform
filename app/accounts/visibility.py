"""
Видимость данных: «что из этого видит пользователь» (часть B4).

Модель: docs/users-model.md, раздел «Видимость данных». Права (роли) отвечают
на вопрос «что человек может делать», видимость - «чьи данные он видит».
Одна функция для всех таблиц страховой: экраны не решают это сами.

Видимость берётся из прикрепления человека, действующего сегодня:

| Кто                                         | Видит                                  |
|---------------------------------------------|----------------------------------------|
| «вся страховая»                             | всё своей страховой                    |
| «своё подразделение», сидит в головном офисе | всё своей страховой (решение 25)       |
| «своё подразделение», руководитель филиала   | филиал и его отделы                    |
| «своё подразделение», остальные             | только своё подразделение (решение 10) |
| «свои»                                      | записи, где он продавец или ввёл их    |
| администратор платформы, пользователь агента | ничего (агент - с кабинетом агента)    |
| нет действующего прикрепления               | ничего                                 |
| суперпользователь без страховой             | всё (аварийная запись)                 |

Как таблица подключается - Scope.filter():

    scope_for(request.user).filter(queryset)
        таблица, видимая всей страховой (контрагенты - решение 3, справочник
        agencyId, подразделения): страховая сотрудника, при любой видимости.

    scope_for(request.user).filter(queryset, department=...)
        таблица, у записи которой есть подразделение (агенты: место агента;
        позже договоры). «Своё подразделение» - только записи подразделений
        из Scope.department_ids; «свои» - только записи, которые задаёт
        owner (у агентов owner нет - куратора нет, решение 26).

department - путь к полю подразделения («department_id») или функция
(номера подразделений, дата) -> условие Q, если подразделение записи
вычисляется (у агента - по месту агента на дату).
"""
import dataclasses
import datetime

from django.db.models import Q
from django.utils import timezone

# Роли, работе которых нужны записи подразделения: с видимостью «свои» такой
# сотрудник не увидел бы ни одной записи, поэтому это сочетание запрещено
# (решение 21.10 по части B4). «agents» - «Работа с агентами»: куратора у агента
# нет (решение 26), и «своих» агентов не бывает.
DEPARTMENT_ROLES = {"agents": "Работа с агентами"}


class Kind:
    NONE = "none"                # ничего
    ALL = "all"                  # всё, все страховые (суперпользователь)
    COMPANY = "company"          # вся своя страховая
    DEPARTMENTS = "departments"  # подразделения из department_ids
    OWN = "own"                  # свои записи


@dataclasses.dataclass(frozen=True)
class Scope:
    """Что видит пользователь. Создаётся функцией scope_for()."""

    kind: str
    company_id: int | None = None
    department_ids: frozenset = frozenset()
    user_id: int | None = None
    day: datetime.date | None = None

    def filter(self, queryset, *, company="insurance_company_id", department=None, owner=None):
        """
        Оставить в queryset только видимые записи.

        company - путь к страховой записи; department - подразделение записи
        (путь или функция, см. начало файла); owner - путь к пользователю,
        чьи это записи (продавец, кто ввёл). Без department таблица видна
        всей страховой при любой видимости.
        """
        if self.kind == Kind.ALL:
            return queryset
        if self.kind == Kind.NONE or self.company_id is None:
            return queryset.none()
        queryset = queryset.filter(**{company: self.company_id})
        if department is None or self.kind == Kind.COMPANY:
            return queryset
        if self.kind == Kind.DEPARTMENTS:
            if callable(department):
                return queryset.filter(department(self.department_ids, self.day))
            return queryset.filter(**{f"{department}__in": self.department_ids})
        # Kind.OWN
        if owner is None:
            return queryset.none()
        return queryset.filter(**{owner: self.user_id})

    def departments(self, queryset):
        """
        Подразделения, которые человек видит (для списков выбора: к какому
        подразделению прикрепить агента). queryset - Department.objects...
        При «свои» - ни одного: записи подразделения ему не видны.
        """
        if self.kind == Kind.ALL:
            return queryset
        if self.kind in (Kind.NONE, Kind.OWN) or self.company_id is None:
            return queryset.none()
        queryset = queryset.filter(insurance_company_id=self.company_id)
        if self.kind == Kind.DEPARTMENTS:
            queryset = queryset.filter(pk__in=self.department_ids)
        return queryset

    @property
    def sees_departments(self):
        """Видит ли записи подразделений (а не только свои или ничего)."""
        return self.kind in (Kind.ALL, Kind.COMPANY, Kind.DEPARTMENTS)


NOTHING = Scope(Kind.NONE)


def visible(request, queryset, **where):
    """Короткая запись для экранов: scope_for(request.user).filter(queryset, ...)."""
    return scope_for(request.user).filter(queryset, **where)


def scope_for(user, day=None):
    """
    Что видит пользователь на дату (по умолчанию - сегодня по часовому поясу
    системы). Каждый вызов читает прикрепление заново (один запрос): перевод
    или смена видимости действуют сразу.
    """
    from companies.models import Department

    from .models import Assignment, User, Visibility

    day = day or timezone.localdate()
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return NOTHING
    company_id = user.insurance_company_id
    if user.is_superuser:
        # Аварийная запись: без страховой - всё; со страховой - вся своя страховая.
        return Scope(Kind.COMPANY, company_id, day=day) if company_id else Scope(Kind.ALL, day=day)
    if company_id is None or user.kind in (User.Kind.PLATFORM, User.Kind.AGENT):
        # Платформа данных страховых не видит; пользователь агента - пока ничего
        # (видимость «свои» и «агентство» придут с кабинетом агента, решение 23).
        return NOTHING
    place = Assignment.objects.on(day).filter(user=user).select_related("department").first()
    if place is None:
        return NOTHING
    if place.visibility == Visibility.COMPANY:
        return Scope(Kind.COMPANY, company_id, day=day)
    if place.visibility == Visibility.OWN:
        return Scope(Kind.OWN, company_id, user_id=user.pk, day=day)
    # «Своё подразделение».
    department = place.department
    if department.kind == Department.Kind.HEAD_OFFICE:
        # Головной офис видит всех (решение 25).
        return Scope(Kind.COMPANY, company_id, day=day)
    ids = {department.pk}
    if department.kind == Department.Kind.BRANCH and place.is_head:
        # Руководитель филиала - и отделы филиала (в том числе закрытые:
        # их записи остаются за филиалом).
        ids |= set(department.children.values_list("pk", flat=True))
    return Scope(Kind.DEPARTMENTS, company_id, department_ids=frozenset(ids), user_id=user.pk, day=day)


def roles_visibility_error(roles, visibility):
    """
    Сочетание ролей и видимости, при котором человек не увидит ни одной
    записи своей работы, - текст ошибки; иначе None (решение по части B4:
    «Работа с агентами» с видимостью «свои» не сохраняется).
    """
    from .models import Visibility

    if visibility != Visibility.OWN:
        return None
    names = [DEPARTMENT_ROLES[role.code] for role in roles if role.code in DEPARTMENT_ROLES]
    if not names:
        return None
    return (
        f"С ролью «{names[0]}» видимость «свои» не подходит: у агентов нет куратора, "
        "и человек не увидит ни одного агента. Выберите «своё подразделение» или «вся страховая»."
    )


# --- Журнал изменений -----------------------------------------------------------------
#
# Журнал (audit.ChangeLog) общий для всех таблиц и видит всю страховую.
# Таблица с видимостью по подразделению регистрирует здесь правило:
# функция (scope) -> условие Q на строки журнала, которые надо скрыть
# (или None). Так audit не зависит от приложений, а приложение (agents)
# само знает, чьи строки скрывать. Регистрация - в AppConfig.ready().

_JOURNAL_RULES = []


def hide_in_journal(rule):
    """Зарегистрировать правило скрытия строк журнала (см. выше)."""
    if rule not in _JOURNAL_RULES:
        _JOURNAL_RULES.append(rule)
    return rule


def journal_hidden(scope):
    """Условие на строки журнала, которые человек не видит; None - всё видно."""
    hidden = None
    for rule in _JOURNAL_RULES:
        condition = rule(scope)
        if condition is not None:
            hidden = condition if hidden is None else hidden | condition
    return hidden
