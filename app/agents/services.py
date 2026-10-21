"""
Действия с агентом: прикрепить к подразделению, перевести, прекратить
соглашение (модель: docs/users-model.md, «Агенты (этап B)»).

Как и кадровые действия с сотрудниками (accounts/personnel.py), они
делаются только через эти функции: экран лишь собирает данные. Каждое
действие - одна транзакция: либо всё, либо ничего; каждая запись
проверяется (full_clean) и пишется в журнал от имени сотрудника.
"""
import datetime

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Agreement, Placement


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
    день после last_day; пользователи агента (часть B3) - тоже.
    """
    if agreement.terminated_on is not None:
        raise ValidationError({"last_day": f"Соглашение уже прекращено {agreement.terminated_on:%d.%m.%Y}."})
    agreement.terminated_on = last_day
    agreement.termination_reason = reason
    try:
        return _save(agreement, by)
    except ValidationError as error:
        # Ошибки дат - к полю «последний день» формы.
        if hasattr(error, "error_dict") and "terminated_on" in error.error_dict:
            raise ValidationError({"last_day": error.error_dict["terminated_on"]}) from error
        raise
