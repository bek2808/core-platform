"""Как показывать файл человеку (экраны и журнал просмотров)."""
from django.utils import timezone


def size_text(size):
    """Размер файла для показа: «340 КБ», «1,2 МБ»."""
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} КБ"
    return f"{size / (1024 * 1024):.1f}".replace(".", ",") + " МБ"


def uploaded_text(stored):
    """«22.10.2026, Бекзод»: когда и кто загрузил (время по часовому поясу системы)."""
    when = timezone.localtime(stored.uploaded_at)
    who = stored.uploaded_by.full_name() if stored.uploaded_by_id else "система"
    return f"{when:%d.%m.%Y}, {who}"
