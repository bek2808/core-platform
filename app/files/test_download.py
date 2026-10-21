"""
Автотесты скачивания файлов и их видимости (часть F3a; модель: docs/files-model.md,
«Скачивание и просмотр»).

Главное, что проверяется:
- скан соглашения открывает тот, кто видит агента: вся страховая и головной
  офис - все сканы; филиал и отдел - только свои (как в agents/test_visibility.py);
- чужая страховая и чужое подразделение - «не найдено» (чужие файлы не
  подтверждаем); администратор платформы и сотрудник без нужных прав - 403;
  без входа - на страницу входа;
- файл отдаётся целиком, под исходным именем, с защитными заголовками:
  nosniff и «не кэшировать»; у картинок - полная песочница, у PDF её нет;
- каждое открытие пишется в журнал просмотров, отказ - нет;
- запись есть, а файла на диске нет - «не найдено» и ошибка в журнале приложения.

Запуск на сервере: bin/test files
"""
import logging
import tempfile
import uuid
from pathlib import Path

from django.test import override_settings

from accounts.models import User, Visibility
from accounts.testing import give_roles
from agents.apps import AGREEMENT_SCAN
from agents.models import Agreement
from agents.test_agents import D
from agents.test_visibility import VisibilityCase
from config.testing import refusal_expected

from . import storage
from .models import FileView
from .tests import JPEG_BYTES, PDF_BYTES, TEST_PURPOSE, upload

# Тестовое назначение (files/tests.py) видит тех же людей, что и скан соглашения.
from files import registry  # noqa: E402  (после импорта tests: там назначение регистрируется)

registry.register(registry.Purpose(
    code="test_photo_visible", label="тестовое фото агента", types=frozenset({registry.JPEG, registry.PDF}),
    max_size=2048, view_perm="agents.view_agreement",
    owners=lambda scope: registry.get(AGREEMENT_SCAN).owners(scope),
))


class DownloadCase(VisibilityCase):
    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        cls._settings = override_settings(FILES_ROOT=Path(cls._folder.name))
        cls._settings.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._settings.disable()
        cls._folder.cleanup()

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.scans = {}
        for name, agent in (("head", cls.at_head), ("division", cls.at_division), ("branch", cls.at_branch),
                            ("branch_sales", cls.at_branch_sales), ("samarkand", cls.at_samarkand),
                            ("foreign", cls.foreign_agent)):
            agreement = Agreement(
                agent=agent, number=f"SCAN/{name}", signed_on=D(2021, 4, 14), valid_from=D(2021, 4, 14),
                signer_name="Халилов О.Н.", signer_position="Генеральный директор",
                signer_basis=Agreement.SignerBasis.CHARTER,
            )
            agreement.full_clean()
            agreement.save()
            company = agent.insurance_company
            cls.scans[name] = storage.store(
                upload(PDF_BYTES + name.encode(), f"Соглашение {name}.pdf"),
                purpose=AGREEMENT_SCAN, owner_id=agreement.pk, company=company, by=cls.director,
            )

    def setUp(self):
        # «Не найдено» здесь - ожидаемый отказ; без этого Django печатает его в вывод
        # тестов предупреждением, и оно выглядит как ошибка (отказы 403 и 405 -
        # через refusal_expected, они сами проверяют запись в журнал).
        logger = logging.getLogger("django.request")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.ERROR)

    def open(self, name_or_record):
        record = self.scans[name_or_record] if isinstance(name_or_record, str) else name_or_record
        return self.client.get(f"/files/{record.pk}/")

    def opens(self, user):
        """Какие сканы человек открывает (200)."""
        self.client.force_login(user)
        result = set()
        for name in self.scans:
            response = self.open(name)
            self.assertIn(response.status_code, (200, 404), name)
            if response.status_code == 200:
                result.add(name)
        return result


class VisibilityDownloadTests(DownloadCase):
    mosaic_scans = {"head", "division", "branch", "branch_sales", "samarkand"}

    def test_whole_company_opens_all_of_its_scans(self):
        user = self.employee("all", self.branch, Visibility.COMPANY)
        self.assertEqual(self.opens(user), self.mosaic_scans)

    def test_head_office_opens_all_of_its_scans(self):
        user = self.employee("ho", self.head, Visibility.DEPARTMENT)
        self.assertEqual(self.opens(user), self.mosaic_scans)

    def test_division_opens_only_its_own(self):
        user = self.employee("it", self.division, Visibility.DEPARTMENT, is_head=True)
        self.assertEqual(self.opens(user), {"division"})

    def test_branch_employee_opens_only_branch(self):
        user = self.employee("clerk", self.branch, Visibility.DEPARTMENT)
        self.assertEqual(self.opens(user), {"branch"})

    def test_branch_head_opens_branch_and_its_divisions(self):
        user = self.employee("boss", self.branch, Visibility.DEPARTMENT, is_head=True)
        self.assertEqual(self.opens(user), {"branch", "branch_sales"})

    def test_other_company_scan_is_not_found_even_with_all_rights(self):
        user = self.employee("all", self.branch, Visibility.COMPANY, roles=("Администратор страховой",))
        self.client.force_login(user)
        self.assertEqual(self.open("foreign").status_code, 404)
        self.assertFalse(FileView.objects.exists())

    def test_other_company_user_does_not_open_ours(self):
        give_roles(self.stranger, "Работа с агентами")
        self.client.force_login(self.stranger)
        for name in self.mosaic_scans:
            self.assertEqual(self.open(name).status_code, 404, name)
        self.assertEqual(self.open("foreign").status_code, 200)

    def test_superuser_without_company_opens_everything(self):
        root = User.objects.create_superuser("root", password="Kx7#mQ2v!pLr9zT")
        self.assertEqual(self.opens(root), set(self.scans))

    def test_platform_administrator_is_refused(self):
        platform = User.objects.create_user("platform", kind=User.Kind.PLATFORM)
        give_roles(platform, "Администратор платформы")
        self.client.force_login(platform)
        with refusal_expected(self):
            self.assertEqual(self.open("branch").status_code, 403)
        self.assertFalse(FileView.objects.exists())

    def test_employee_without_agent_rights_is_refused(self):
        user = self.employee("plain", self.branch, Visibility.COMPANY, roles=("Сотрудник",))
        self.client.force_login(user)
        with refusal_expected(self):
            self.assertEqual(self.open("branch").status_code, 403)

    def test_agent_user_is_refused(self):
        agent_user = User.objects.create_user("agent_user", insurance_company=self.mosaic, kind=User.Kind.AGENT)
        self.client.force_login(agent_user)
        # Пользователь агента без прикрепления в систему не входит вовсе (сессия не признаётся).
        response = self.open("branch")
        self.assertIn(response.status_code, (302, 403, 404))
        self.assertNotEqual(response.status_code, 200)

    def test_anonymous_goes_to_login(self):
        response = self.open("branch")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])
        self.assertFalse(FileView.objects.exists())

    def test_transfer_moves_visibility_of_the_scan(self):
        from agents import services

        services.transfer(self.at_branch, department=self.samarkand, valid_from=D(2099, 1, 1), by=self.director)
        jizzakh = self.employee("clerk", self.branch, Visibility.DEPARTMENT)
        samarkand = self.employee("clerk2", self.samarkand, Visibility.DEPARTMENT)
        # До даты перевода агента видят оба подразделения (agents_placed_in) - скан тоже.
        self.assertIn("branch", self.opens(jizzakh))
        self.assertIn("branch", self.opens(samarkand))


class DownloadResponseTests(DownloadCase):
    def setUp(self):
        super().setUp()
        self.user = self.employee("all", self.branch, Visibility.COMPANY)
        self.client.force_login(self.user)

    def test_pdf_is_served_whole_with_protective_headers(self):
        record = self.scans["branch"]
        response = self.open(record)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), PDF_BYTES + b"branch")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        # Открывается во вкладке (не «сохранить как»), под исходным именем.
        disposition = response["Content-Disposition"]
        self.assertTrue(disposition.startswith("inline"), disposition)
        self.assertIn("filename*=utf-8''%D0%A1%D0%BE%D0%B3%D0%BB%D0%B0%D1%88%D0%B5%D0%BD%D0%B8%D0%B5", disposition)
        # У PDF песочницы нет: встроенный просмотр Chrome в ней не работает.
        self.assertNotIn("Content-Security-Policy", response)

    def test_image_gets_full_sandbox(self):
        agreement = Agreement.objects.get(number="SCAN/branch")
        photo = storage.store(upload(JPEG_BYTES, "фото.jpg"), purpose="test_photo_visible",
                              owner_id=agreement.pk, company=self.mosaic, by=self.user)
        response = self.open(photo)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(response["Content-Security-Policy"], "sandbox")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_every_open_is_logged_and_refusal_is_not(self):
        self.open("branch")
        self.open("branch")
        self.open("branch_sales")
        views = FileView.objects.filter(file=self.scans["branch"])
        self.assertEqual(views.count(), 2)
        self.assertEqual({view.user for view in views}, {self.user})
        self.assertEqual(FileView.objects.count(), 3)
        self.client.force_login(self.employee("clerk", self.samarkand, Visibility.DEPARTMENT))
        self.assertEqual(self.open("branch").status_code, 404)
        self.assertEqual(FileView.objects.count(), 3)

    def test_unknown_number_is_not_found(self):
        self.assertEqual(self.client.get(f"/files/{uuid.uuid4()}/").status_code, 404)
        self.assertEqual(self.client.get("/files/not-a-number/").status_code, 404)

    def test_only_get_is_allowed(self):
        with refusal_expected(self):
            self.assertEqual(self.client.post(f"/files/{self.scans['branch'].pk}/").status_code, 405)

    def test_missing_file_on_disk_is_not_found_and_logged(self):
        record = self.scans["samarkand"]
        path = storage.open_path(record)
        saved = path.read_bytes()
        path.unlink()
        try:
            with self.assertLogs("files.views", level="ERROR") as logs:
                self.assertEqual(self.open(record).status_code, 404)
            self.assertIn(str(record.pk), logs.output[0])
            self.assertFalse(FileView.objects.exists())
        finally:
            path.write_bytes(saved)  # файлы лежат на весь класс: вернуть для следующих тестов

    def test_unused_test_purpose_is_registered(self):
        # Назначение из files/tests.py не мешает: его записи не видит никто.
        self.assertEqual(registry.get(TEST_PURPOSE).code, TEST_PURPOSE)
