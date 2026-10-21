"""
Скачивание файла: /files/<номер>/ (docs/files-model.md).

Прямой ссылки на файл на диске нет: файл отдаёт только эта страница, после
проверки прав. Порядок проверок:
1. не вошёл - на страницу входа;
2. файла с таким номером нет - «не найдено»;
3. у человека нет права смотреть записи этого назначения (администратор
   платформы, пользователь агента) - 403;
4. запись, к которой приложен файл, ему не видна (чужая страховая, чужое
   подразделение) - «не найдено»: чужие файлы не подтверждаем.
Каждое открытие пишется в журнал просмотров.
"""
import logging

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.views.decorators.http import require_GET

from accounts.visibility import scope_for

from . import registry, storage
from .models import FileView, StoredFile

log = logging.getLogger(__name__)


def visible_to(user, stored):
    """Видит ли пользователь запись, к которой приложен файл."""
    rule = registry.get(stored.purpose)
    scope = scope_for(user)
    if not scope.filter(StoredFile.objects.filter(pk=stored.pk)).exists():
        return False  # другая страховая
    return rule.owners(scope).filter(pk=stored.owner_id).exists()


@require_GET
def download(request, pk):
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    stored = StoredFile.objects.filter(pk=pk).first()
    if stored is None:
        raise Http404("Файл не найден")
    rule = registry.get(stored.purpose)
    if not request.user.has_perm(rule.view_perm):
        raise PermissionDenied
    if not visible_to(request.user, stored):
        raise Http404("Файл не найден")
    try:
        path = storage.open_path(stored)
    except FileNotFoundError:
        # Запись есть, файла нет: серьёзно, нужна проверка files_check и копия.
        log.error("Файл %s (%s) есть в базе, но не найден на диске", stored.pk, stored.path)
        raise Http404("Файл не найден") from None

    FileView(file=stored, user=request.user).save()
    response = FileResponse(
        open(path, "rb"), content_type=stored.content_type, as_attachment=False,
        filename=stored.original_name,
    )
    # Браузер не должен «угадывать» тип и выполнять содержимое; копий в общих
    # кэшах (прокси) быть не должно: в сканах бывают паспортные данные.
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    if stored.content_type != registry.PDF:
        # Для картинок - полная песочница. Для PDF её нельзя: встроенный
        # просмотр PDF в Chrome в песочнице не работает (пустая страница).
        response["Content-Security-Policy"] = "sandbox"
    return response
