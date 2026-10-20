"""
Приведение введённых значений к единому виду.

Данные приходят от людей и от провайдера ЕРСП с «мусором»: пробелы в конце
(«12-uy  »), двойные пробелы внутри («Choʻlpon MFY,  Elbek»), неразрывные
пробелы из Word и Excel. Эти функции убирают мусор, но не меняют смысл.
"""
import re

_SPACES = re.compile(r"\s+")


def squash_spaces(value):
    """Пробелы по краям убрать, подряд идущие пробелы внутри заменить одним."""
    if value is None:
        return value
    return _SPACES.sub(" ", value).strip()


def remove_spaces(value):
    """Убрать все пробелы: для кодов и номеров (МФО, ИНН, счёт)."""
    if value is None:
        return value
    return _SPACES.sub("", value)
