"""
Автотесты журнала просмотров файлов в админ-панели (часть F3b; files/admin.py).

Главное, что проверяется:
- администратор страховой (видимость «вся страховая») видит просмотры файлов
  своей страховой: кто, когда, какой файл, что это;
- просмотры чужой страховой не видны; при видимости «своё подразделение»
  журнал пуст (он общий для всех файлов); администратору платформы и тому,
  у кого нет права, - 403;
- журнал только для чтения: добавить, изменить и удалить нельзя никому.

Запуск на сервере: bin/test files.test_journal
"""
from accounts.models import User, Visibility
from accounts.testing import give_roles
from config.testing import refusal_expected

from .models import FileView
from .test_download import DownloadCase

LIST = "/admin/files/fileview/"
ADMIN_ROLE = "Администратор страховой"


class ViewsJournalTests(DownloadCase):
    def setUp(self):
        super().setUp()
        self.admin = self.employee("admin", self.branch, Visibility.COMPANY, roles=(ADMIN_ROLE,))

    def open_files(self, user, *names):
        self.client.force_login(user)
        for name in names:
            self.assertEqual(self.open(name).status_code, 200, name)

    def test_company_administrator_sees_who_opened_what(self):
        self.open_files(self.admin, "branch", "head")
        page = self.client.get(LIST)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Соглашение branch.pdf")
        self.assertContains(page, "Соглашение head.pdf")
        self.assertContains(page, "Admin")                                   # кто
        self.assertContains(page, "скан агентского соглашения №")            # что это
        self.assertEqual(page.context["cl"].result_count, 2)

    def test_other_company_views_are_not_shown(self):
        root = User.objects.create_superuser("root", password="Kx7#mQ2v!pLr9zT")
        self.open_files(root, "foreign", "branch")
        self.open_files(self.admin, "head")
        page = self.client.get(LIST)
        names = {view.file.original_name for view in page.context["cl"].queryset}
        self.assertEqual(names, {"Соглашение head.pdf", "Соглашение branch.pdf"})  # без foreign
        self.assertEqual(FileView.objects.count(), 3)

    def test_department_visibility_sees_nothing(self):
        boss = self.employee("boss", self.branch, Visibility.DEPARTMENT, is_head=True, roles=(ADMIN_ROLE,))
        self.open_files(boss, "branch")
        page = self.client.get(LIST)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["cl"].result_count, 0)

    def test_platform_administrator_is_refused(self):
        self.open_files(self.admin, "branch")
        platform = User.objects.create_user("platform", kind=User.Kind.PLATFORM)
        give_roles(platform, "Администратор платформы")
        self.client.force_login(platform)
        with refusal_expected(self):
            self.assertEqual(self.client.get(LIST).status_code, 403)

    def test_employee_without_the_right_is_refused(self):
        clerk = self.employee("clerk", self.branch, Visibility.COMPANY)  # «Работа с агентами»
        self.client.force_login(clerk)
        with refusal_expected(self):
            self.assertEqual(self.client.get(LIST).status_code, 403)

    def test_journal_is_read_only_for_everyone(self):
        self.open_files(self.admin, "branch")
        view = FileView.objects.get()
        root = User.objects.create_superuser("root", password="Kx7#mQ2v!pLr9zT")
        for user in (self.admin, root):
            self.client.force_login(user)
            for url, method in ((f"{LIST}add/", "get"), (f"{LIST}{view.pk}/change/", "post"),
                                (f"{LIST}{view.pk}/delete/", "post")):
                with refusal_expected(self):
                    response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 403, (user.username, url))
        self.assertEqual(FileView.objects.count(), 1)
