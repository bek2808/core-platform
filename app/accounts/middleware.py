"""
Обязательная смена временного пароля (часть A3, docs/users-model.md).

Временный пароль выдаёт система (новый пользователь, «Сбросить пароль»)
и ставит отметку «сменить пароль при входе». Пока она стоит, в админ-панели
открывается только страница смены своего пароля (и выход): всё остальное
переадресуется туда. Отметку снимает сама смена пароля
(accounts.admin.OwnPasswordChangeForm).
"""
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

ALLOWED_PAGES = ["admin:password_change", "admin:password_change_done", "admin:logout", "admin:jsi18n"]


class PasswordChangeRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and getattr(user, "must_change_password", False)
            and request.path.startswith(reverse("admin:index"))
            and request.path not in {reverse(name) for name in ALLOWED_PAGES}
        ):
            messages.warning(request, "Вы вошли по временному паролю. Задайте свой пароль, чтобы продолжить.")
            return redirect("admin:password_change")
        return self.get_response(request)
