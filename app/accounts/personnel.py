"""
Кадровые действия с пользователем страховой (часть A3, docs/users-model.md):
принять, перевести, уволить, выдать временный пароль - и правило
«в страховой всегда есть администратор».

Экран (accounts/admin.py) только собирает данные и вызывает эти функции:
так правила одни и те же, откуда бы ни пришло действие.

Каждое действие - целиком или никак (transaction.atomic): если что-то
не прошло проверку, ничего не меняется. Ошибки - ValidationError
с понятным текстом; ключи ошибок совпадают с полями форм экрана.
"""
import datetime
import secrets
import string

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Assignment

COMPANY_ADMIN_ROLE = "company_admin"

# Временный пароль: без похожих знаков (0/O, 1/l/I), чтобы его можно было
# продиктовать или переписать без ошибок. Три группы по четыре знака.
_ALPHABET = "".join(ch for ch in string.ascii_letters + string.digits if ch not in "0O1lI")


def generate_password(user=None):
    """Случайный пароль, который проходит правила сложности системы."""
    while True:
        password = "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3))
        if not (any(c.isdigit() for c in password) and any(c.isupper() for c in password)
                and any(c.islower() for c in password)):
            continue
        try:
            validate_password(password, user)
        except ValidationError:
            continue
        return password


def issue_temporary_password(user, by):
    """
    Выдать временный пароль: прежний перестаёт действовать, при первом входе
    человек обязан задать свой. Журнал: «пароль: ***», автор - by.
    Возвращает пароль - его показывают администратору один раз и нигде не хранят.
    """
    password = generate_password(user)
    user.set_password(password)
    user.must_change_password = True
    user.save(by=by)
    return password


# --- «В страховой всегда есть администратор» -------------------------------------


class Period:
    """Прикрепление для проверки: даты и есть ли в нём роль администратора страховой."""

    def __init__(self, valid_from, valid_to, is_admin):
        self.valid_from, self.valid_to, self.is_admin = valid_from, valid_to, is_admin

    def covers(self, day):
        return self.valid_from <= day and (self.valid_to is None or day <= self.valid_to)


def is_admin_role_set(roles):
    return any(role.code == COMPANY_ADMIN_ROLE for role in roles)


def periods_of(user):
    """Прикрепления человека в базе (как они есть сейчас)."""
    if user.pk is None:
        return []
    return [
        Period(a.valid_from, a.valid_to, is_admin_role_set(a.roles.all()))
        for a in Assignment.objects.filter(user_id=user.pk).prefetch_related("roles")
    ]


def _admin_on(periods, is_active, day):
    return is_active and any(p.is_admin and p.covers(day) for p in periods)


def _others_are_admins(company_id, user_id, day):
    return (
        Assignment.objects.on(day)
        .filter(user__insurance_company_id=company_id, user__is_active=True, roles__code=COMPANY_ADMIN_ROLE)
        .exclude(user_id=user_id)
        .exists()
    )


def check_admin_remains(user, periods_after, is_active_after):
    """
    Не оставит ли изменение страховую без администратора.

    Сравнивает «до» (база) и «после» (periods_after, is_active_after) на сегодня
    и на каждую будущую дату, когда меняются прикрепления страховой: между
    этими датами состав администраторов не меняется. Ошибка - только если
    в какой-то день человек был единственным администратором и перестаёт им
    быть. Страховую, где администратора ещё нет, правило не блокирует.
    """
    company_id = user.insurance_company_id
    if company_id is None or user.pk is None:
        return
    periods_before = periods_of(user)
    is_active_before = type(user).objects.filter(pk=user.pk).values_list("is_active", flat=True).first()
    today = timezone.localdate()

    days = {today}
    company_periods = Assignment.objects.filter(user__insurance_company_id=company_id).values_list(
        "valid_from", "valid_to"
    )
    own = [(p.valid_from, p.valid_to) for p in [*periods_before, *periods_after]]
    for valid_from, valid_to in [*company_periods, *own]:
        if valid_from and valid_from > today:
            days.add(valid_from)
        if valid_to and valid_to >= today:
            days.add(valid_to + datetime.timedelta(days=1))

    for day in sorted(days):
        if not _admin_on(periods_before, is_active_before, day):
            continue
        if _admin_on(periods_after, is_active_after, day):
            continue
        if _others_are_admins(company_id, user.pk, day):
            continue
        since = "" if day == today else f" с {day:%d.%m.%Y}"
        raise ValidationError(
            f"В страховой не останется администратора{since}: {user.full_name()} - "
            "единственный пользователь с ролью «Администратор страховой». Сначала дайте эту роль "
            "другому сотруднику."
        )


# --- Принять, перевести, уволить ---------------------------------------------------


def _later_start(user, day):
    return Assignment.objects.filter(user_id=user.pk, valid_from__gt=day).order_by("valid_from").first()


def _new_assignment(user, place, valid_from, valid_to=None):
    return Assignment(
        user=user,
        department=place["department"],
        position=place["position"],
        visibility=place.get("visibility") or Assignment._meta.get_field("visibility").default,
        is_head=place.get("is_head", False),
        basis=place.get("basis", ""),
        valid_from=valid_from,
        valid_to=valid_to,
    )


def _save_new(assignment, roles, by):
    # full_clean проверяет и правила базы (одно место на дату, один
    # руководитель подразделения) - ошибка придёт сообщением, а не 500.
    assignment.full_clean()
    assignment.save(by=by)
    assignment.set_roles(roles, by=by)
    return assignment


@transaction.atomic
def hire(user, *, valid_from, place, roles, by, valid_to=None):
    """
    Принять: новое место с даты valid_from (у человека нет места на эту дату
    и позже). Заблокированная учётная запись разблокируется - вход откроется
    с даты начала (accounts/backends.py).
    """
    roles = list(roles)
    busy = Assignment.objects.filter(user_id=user.pk).filter(
        Q(valid_to__isnull=True) | Q(valid_to__gte=valid_from)
    ).first()
    if busy is not None:
        raise ValidationError({"valid_from": (
            f"У человека уже есть место на эту дату или позже ({busy.department.full_name()}, "
            f"с {busy.valid_from:%d.%m.%Y}). Чтобы сменить место - «Перевести»."
        )})
    assignment = _save_new(_new_assignment(user, place, valid_from, valid_to), roles, by)
    if not user.is_active:
        user.is_active = True
        user.save(by=by)
    return assignment


@transaction.atomic
def transfer(user, *, valid_from, place, roles, by):
    """
    Перевести с даты valid_from: нынешнее место закрывается накануне, новое
    открывается с этой даты; «по» нынешнего места (если было) переходит к новому.
    """
    roles = list(roles)
    current = Assignment.objects.on(valid_from).filter(user_id=user.pk).first()
    if current is None:
        raise ValidationError({"valid_from": "На эту дату у человека нет места. Чтобы дать место - «Принять»."})
    if valid_from <= current.valid_from:
        raise ValidationError({"valid_from": (
            f"Нынешнее место действует с {current.valid_from:%d.%m.%Y}: перевод - не раньше следующего дня."
        )})
    later = _later_start(user, valid_from)
    if later is not None:
        raise ValidationError({"valid_from": (
            f"У человека есть место, которое начнётся позже - с {later.valid_from:%d.%m.%Y}. "
            "Переводить можно только с последнего места."
        )})

    end = current.valid_to
    others = [p for p in periods_of(user) if not (p.valid_from == current.valid_from)]
    check_admin_remains(user, [
        *others,
        Period(current.valid_from, valid_from - datetime.timedelta(days=1),
               is_admin_role_set(current.roles.all())),
        Period(valid_from, end, is_admin_role_set(roles)),
    ], user.is_active)

    current.valid_to = valid_from - datetime.timedelta(days=1)
    current.save(by=by)
    return _save_new(_new_assignment(user, place, valid_from, end), roles, by)


@transaction.atomic
def dismiss(user, *, last_day, by):
    """
    Уволить: место закрывается последним днём работы. Если этот день сегодня
    или прошёл - учётная запись блокируется сразу; если впереди - вход
    закроется сам на следующий день (нет действующего места).
    """
    current = Assignment.objects.on(last_day).filter(user_id=user.pk).first()
    if current is None:
        raise ValidationError({"last_day": "На эту дату у человека нет места."})
    later = _later_start(user, last_day)
    if later is not None:
        raise ValidationError({"last_day": (
            f"У человека есть место, которое начнётся позже - с {later.valid_from:%d.%m.%Y}."
        )})

    block_now = last_day <= timezone.localdate()
    others = [p for p in periods_of(user) if p.valid_from != current.valid_from]
    check_admin_remains(user, [
        *others, Period(current.valid_from, last_day, is_admin_role_set(current.roles.all())),
    ], user.is_active and not block_now)

    current.valid_to = last_day
    current.save(by=by)
    if block_now and user.is_active:
        user.is_active = False
        user.save(by=by)
    return current
