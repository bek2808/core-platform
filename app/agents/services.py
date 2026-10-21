"""
Действия с агентом: прикрепить к подразделению, перевести, прекратить
соглашение; завести и отключить пользователя агента (модель:
docs/users-model.md, «Агенты (этап B)»).

Как и кадровые действия с сотрудниками (accounts/personnel.py), они
делаются только через эти функции: экран лишь собирает данные. Каждое
действие - одна транзакция: либо всё, либо ничего; каждая запись
проверяется (full_clean) и пишется в журнал от имени сотрудника.
"""
import datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts import personnel
from accounts.models import User

from .models import Agreement, Member, Placement


def _save(record, by):
    record.full_clean()
    record.save(by=by)
    return record


@transaction.atomic
def place(agent, *, department, valid_from, by, basis=""):
    """
    Прикрепить агента к подразделению с даты. Для агента без места на эту
    дату и позже (новый агент или место закончилось); если место есть -
    это перевод.
    """
    if agent.placements.filter(valid_from__gte=valid_from).exists() or agent.placements.on(valid_from).exists():
        raise ValidationError({"valid_from": "На эту дату или позже у агента уже есть место. Нужен перевод."})
    return _save(Placement(agent=agent, department=department, valid_from=valid_from, basis=basis), by)


@transaction.atomic
def transfer(agent, *, department, valid_from, by, basis=""):
    """
    Перевести агента с даты valid_from: нынешнее место закрывается накануне,
    новое открывается с этой даты; «по» нынешнего места (если было)
    переходит к новому.
    """
    current = agent.placements.on(valid_from).first()
    if current is None:
        raise ValidationError({"valid_from": "На эту дату у агента нет места. Чтобы дать место - «Прикрепить»."})
    if valid_from <= current.valid_from:
        raise ValidationError({"valid_from": (
            f"Нынешнее место действует с {current.valid_from:%d.%m.%Y}: перевод - не раньше следующего дня."
        )})
    if agent.placements.filter(valid_from__gt=valid_from).exists():
        raise ValidationError({"valid_from": "У агента есть место, которое начнётся позже. Переводить можно только с последнего места."})
    if department.pk == current.department_id:
        raise ValidationError({"department": "Агент уже прикреплён к этому подразделению."})

    end = current.valid_to
    current.valid_to = valid_from - datetime.timedelta(days=1)
    _save(current, by)
    return _save(Placement(agent=agent, department=department, valid_from=valid_from, valid_to=end, basis=basis), by)


@transaction.atomic
def terminate(agreement, *, last_day, reason, by):
    """
    Прекратить соглашение досрочно: последний день и причина. «По» из
    договора остаётся как было. Агент перестаёт «работать» на следующий
    день после last_day; его пользователи не входят со следующего дня,
    а если last_day сегодня или прошёл - блокируются сразу.
    """
    if agreement.terminated_on is not None:
        raise ValidationError({"last_day": f"Соглашение уже прекращено {agreement.terminated_on:%d.%m.%Y}."})
    agreement.terminated_on = last_day
    agreement.termination_reason = reason
    try:
        _save(agreement, by)
    except ValidationError as error:
        # Ошибки дат - к полю «последний день» формы.
        if hasattr(error, "error_dict") and "terminated_on" in error.error_dict:
            raise ValidationError({"last_day": error.error_dict["terminated_on"]}) from error
        raise
    if last_day <= timezone.localdate():
        # Последний день сегодня или прошёл - пользователи агента блокируются
        # сразу (иначе вход закроется сам на следующий день после него).
        for member in agreement.agent.members.on(last_day).select_related("user"):
            _block(member.user, by)
    return agreement


def _block(user, by):
    if user.is_active:
        user.is_active = False
        user.save(by=by)


@transaction.atomic
def add_member(agent, *, username, last_name, first_name, by, valid_from, patronymic="", phone="",
               email="", is_director=False):
    """
    Завести пользователя агента: учётная запись вида «агент» в страховой
    агента и связь с агентом с даты. Пароль придумывает система (как
    у сотрудников): возвращается (связь, временный пароль) - пароль
    показывают один раз и нигде не хранят; при первом входе человек
    задаёт свой.
    """
    user = User(
        username=username, last_name=last_name, first_name=first_name, patronymic=patronymic,
        phone=phone, email=email, kind=User.Kind.AGENT, insurance_company_id=agent.insurance_company_id,
    )
    password = personnel.generate_password(user)
    user.set_password(password)
    user.must_change_password = True
    user.full_clean(exclude=["password"])
    user.save(by=by)
    member = _save(Member(user=user, agent=agent, is_director=is_director, valid_from=valid_from), by)
    return member, password


@transaction.atomic
def disable_member(member, *, last_day, by):
    """
    Отключить пользователя агента: связь закрывается последним днём.
    Если этот день сегодня или прошёл - учётная запись блокируется сразу;
    если впереди - вход закроется сам на следующий день после него.
    Учётная запись не удаляется: продажи остаются за человеком.
    """
    if member.valid_to is not None and member.valid_to < last_day:
        raise ValidationError({"last_day": f"Связь уже закрыта {member.valid_to:%d.%m.%Y}."})
    if last_day < member.valid_from:
        raise ValidationError({"last_day": f"Связь действует с {member.valid_from:%d.%m.%Y}: последний день - не раньше."})
    member.valid_to = last_day
    _save(member, by)
    if last_day <= timezone.localdate():
        _block(member.user, by)
    return member
