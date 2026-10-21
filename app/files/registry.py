"""
Назначения файлов: что за файл, какие типы и какого размера разрешены,
и кто его видит (docs/files-model.md).

Приложение «files» ничего не знает об агентах, договорах и убытках. Каждое
приложение, которому нужны файлы, регистрирует своё назначение в
AppConfig.ready() и говорит три вещи:

- какие типы и какого размера принимаются (types, max_size);
- к каким записям приложен файл и кто их видит (owners): функция
  (что видит пользователь) -> queryset записей. Файл видит тот, кто видит
  запись, к которой он приложен; отдельных прав на файлы нет;
- какое право нужно, чтобы вообще смотреть эти записи (view_perm).

Пример - agents/apps.py (скан агентского соглашения).
"""
import dataclasses
from typing import Callable

MB = 1024 * 1024

PDF = "application/pdf"
JPEG = "image/jpeg"
PNG = "image/png"

# Тип файла -> окончание имени на диске и название для сообщений.
KINDS = {
    PDF: ("pdf", "PDF"),
    JPEG: ("jpg", "JPG"),
    PNG: ("png", "PNG"),
}

# Как начинается файл каждого типа. Тип определяется по содержимому,
# а не по окончанию имени: «вирус.exe», переименованный в «скан.pdf»,
# не пройдёт.
SIGNATURES = (
    (b"%PDF-", PDF),
    (b"\xff\xd8\xff", JPEG),
    (b"\x89PNG\r\n\x1a\n", PNG),
)
SIGNATURE_LENGTH = max(len(prefix) for prefix, _ in SIGNATURES)


def sniff(head):
    """Тип файла по его первым байтам; None - не наш тип."""
    for prefix, mime in SIGNATURES:
        if head.startswith(prefix):
            return mime
    return None


@dataclasses.dataclass(frozen=True)
class Purpose:
    code: str
    label: str
    types: frozenset
    max_size: int
    # (scope) -> queryset записей, которые человек видит (accounts.visibility).
    owners: Callable
    # Право, без которого файлы этого назначения не открываются (например,
    # "agents.view_agreement"): у администратора платформы его нет.
    view_perm: str

    def allowed_names(self):
        return ", ".join(KINDS[mime][1] for mime in KINDS if mime in self.types)


_PURPOSES = {}


def register(purpose):
    """Зарегистрировать назначение (повторная регистрация того же - не ошибка)."""
    known = _PURPOSES.get(purpose.code)
    if known is not None and known != purpose:
        raise ValueError(f"Назначение файлов «{purpose.code}» уже зарегистрировано иначе")
    _PURPOSES[purpose.code] = purpose
    return purpose


def get(code):
    try:
        return _PURPOSES[code]
    except KeyError:
        raise KeyError(f"Неизвестное назначение файлов: «{code}»") from None
