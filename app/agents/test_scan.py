"""
Автотесты скана агентского соглашения (часть F3b; модель: docs/files-model.md,
«Экран: скан в карточке соглашения»).

Главное, что проверяется:
- загрузка и замена: новый файл становится текущим, прежний остаётся
  в версиях и открывается; смена ссылки пишется в журнал соглашения
  (было -> стало), от имени того, кто загрузил;
- не подошёл файл (не PDF, пустой, больше 5 МБ, без файла) - понятная ошибка
  на экране, ничего не меняется и на диске не остаётся;
- скан доступен и у прекращённого соглашения;
- база не пускает скан чужого соглашения, чужой страховой и чужого назначения;
- экран: карточка показывает ссылку, «нет скана», «Загрузить / Заменить скан»
  и «Версии»; страница загрузки открывается только тем, кто видит агента
  и может менять соглашения (платформа - 403, «Сотрудник» - 403, чужой
  агент - «не найдено»);
- слишком большой запрос отклоняется до разбора (413).

Запуск на сервере: bin/test agents.test_scan
"""
import tempfile
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, transaction
from django.test import override_settings

from accounts.models import User, Visibility
from accounts.testing import give_roles
from audit.models import ChangeLog
from config.testing import refusal_expected
from files import storage
from files.models import FileView, StoredFile
from files.registry import MB
from files.tests import JPEG_BYTES, PDF_BYTES, TEST_PURPOSE, upload

from . import services
from .apps import AGREEMENT_SCAN
from .models import Agreement
from .test_agents import AGENTS_URL, D
from .test_visibility import VisibilityCase


class ScanCase(VisibilityCase):
    """Mosaic из VisibilityCase; у агентов филиала и головного офиса - по соглашению."""

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
        cls.branch_agreement = cls.sign(cls.at_branch, "SCAN/branch")
        cls.head_agreement = cls.sign(cls.at_head, "SCAN/head")
        cls.foreign_agreement = cls.sign(cls.foreign_agent, "SCAN/foreign")

    @classmethod
    def sign(cls, agent, number):
        agreement = Agreement(
            agent=agent, number=number, signed_on=D(2021, 4, 14), valid_from=D(2021, 4, 14),
            signer_name="Халилов О.Н.", signer_position="Генеральный директор",
            signer_basis=Agreement.SignerBasis.CHARTER,
        )
        agreement.full_clean()
        agreement.save()
        return agreement

    def attach(self, agreement=None, content=PDF_BYTES, name="Соглашение.pdf", by=None):
        return services.attach_scan(agreement or self.branch_agreement, upload(content, name),
                                    by=by or self.director)

    def scan_rows(self, agreement):
        return ChangeLog.objects.filter(object_id=agreement.pk, field="scan",
                                        content_type__model="agreement").order_by("id")


class ScanServiceTests(ScanCase):
    def test_attach_makes_it_current_and_journals_it(self):
        stored = self.attach()
        self.branch_agreement.refresh_from_db()
        self.assertEqual(self.branch_agreement.scan, stored)
        self.assertEqual((stored.purpose, stored.owner_id), (AGREEMENT_SCAN, self.branch_agreement.pk))
        self.assertEqual((stored.insurance_company, stored.uploaded_by), (self.mosaic, self.director))
        self.assertEqual(stored.original_name, "Соглашение.pdf")
        (row,) = self.scan_rows(self.branch_agreement)
        self.assertEqual((row.old_value, row.new_value, row.user), (None, str(stored.pk), self.director))
        self.assertEqual(row.root_object_id, self.at_branch.pk)  # видна в «Истории» агента

    def test_replace_keeps_old_version(self):
        first = self.attach(name="v1.pdf")
        second = self.attach(content=PDF_BYTES + b"new", name="v2.pdf")
        self.branch_agreement.refresh_from_db()
        self.assertEqual(self.branch_agreement.scan, second)
        self.assertEqual(list(services.agreement_scans(self.branch_agreement)), [second, first])
        self.assertTrue(storage.open_path(first).is_file())
        self.assertEqual(storage.open_path(first).read_bytes(), PDF_BYTES)
        rows = list(self.scan_rows(self.branch_agreement))
        self.assertEqual([(r.old_value, r.new_value) for r in rows],
                         [(None, str(first.pk)), (str(first.pk), str(second.pk))])

    def test_versions_belong_to_their_agreement(self):
        mine = self.attach()
        other = self.attach(self.head_agreement, name="head.pdf")
        self.assertEqual(list(services.agreement_scans(self.branch_agreement)), [mine])
        self.assertEqual(list(services.agreement_scans(self.head_agreement)), [other])

    def test_rejected_file_changes_nothing(self):
        too_big = PDF_BYTES + b"0" * (5 * MB)
        for content, text in ((JPEG_BYTES, "только PDF"), (b"", "пустой"), (b"MZ\x90\x00", "только PDF"),
                              (too_big, "больше 5 МБ")):
            with self.assertRaises(ValidationError) as caught:
                self.attach(content=content, name="скан.pdf")
            self.assertIn(text, caught.exception.message_dict["file"][0])
        self.branch_agreement.refresh_from_db()
        self.assertIsNone(self.branch_agreement.scan)
        self.assertFalse(StoredFile.objects.exists())
        self.assertFalse(self.scan_rows(self.branch_agreement).exists())

    def test_terminated_agreement_takes_a_scan(self):
        services.terminate(self.branch_agreement, last_day=D(2022, 1, 1), reason="Истёк срок", by=self.director)
        stored = self.attach()
        self.branch_agreement.refresh_from_db()
        self.assertEqual(self.branch_agreement.scan, stored)
        self.assertEqual(self.branch_agreement.terminated_on, D(2022, 1, 1))  # прочее не тронуто

    def test_database_refuses_foreign_scans(self):
        def refused(agreement, stored):
            agreement.scan = stored
            with self.assertRaises(DatabaseError), transaction.atomic():
                agreement.save(update_fields=["scan"])

        mine = self.attach()
        other_agreement_scan = self.attach(self.head_agreement, name="head.pdf")
        foreign_scan = storage.store(upload(name="foreign.pdf"), purpose=AGREEMENT_SCAN,
                                     owner_id=self.foreign_agreement.pk, company=self.other)
        wrong_purpose = storage.store(upload(name="photo.pdf"), purpose=TEST_PURPOSE,
                                      owner_id=self.branch_agreement.pk, company=self.mosaic)
        # Скан другого соглашения, чужой страховой и чужого назначения.
        refused(self.branch_agreement, other_agreement_scan)
        refused(self.branch_agreement, foreign_scan)
        refused(self.branch_agreement, wrong_purpose)
        refused(self.foreign_agreement, mine)
        # Свой скан - проходит.
        self.branch_agreement.scan = mine
        self.branch_agreement.save(update_fields=["scan"])


class ScanScreenTests(ScanCase):
    def setUp(self):
        self.boss = self.employee("boss", self.branch, Visibility.DEPARTMENT, is_head=True)
        self.client.force_login(self.boss)

    def scan_url(self, agreement=None):
        agreement = agreement or self.branch_agreement
        return f"{AGENTS_URL}{agreement.agent_id}/agreements/{agreement.pk}/scan/"

    def card(self, agent=None):
        return self.client.get(f"{AGENTS_URL}{(agent or self.at_branch).pk}/change/")

    def post(self, content=PDF_BYTES, name="Соглашение №5.pdf", agreement=None, follow=False):
        return self.client.post(self.scan_url(agreement), {"file": upload(content, name)}, follow=follow)

    def test_card_without_scan(self):
        page = self.card()
        self.assertContains(page, "нет скана")
        self.assertContains(page, "Загрузить скан")
        self.assertContains(page, self.scan_url())
        self.assertNotContains(page, "Версии (")

    def test_upload_page_opens(self):
        page = self.client.get(self.scan_url())
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'enctype="multipart/form-data"')
        self.assertContains(page, "PDF до 5 МБ")
        self.assertContains(page, "Загрузить скан")

    def test_upload_through_the_screen(self):
        page = self.post(follow=True)
        self.assertRedirects(page, f"{AGENTS_URL}{self.at_branch.pk}/change/")
        self.assertContains(page, "скан соглашения")
        self.branch_agreement.refresh_from_db()
        scan = self.branch_agreement.scan
        self.assertEqual((scan.original_name, scan.uploaded_by), ("Соглашение №5.pdf", self.boss))
        # Карточка: ссылка на файл, кто и когда, «Заменить скан».
        self.assertContains(page, f'href="/files/{scan.pk}/"')
        self.assertContains(page, "Соглашение №5.pdf")
        self.assertContains(page, "Заменить скан")
        self.assertNotContains(page, "нет скана")
        # И файл по ссылке открывается тем же человеком.
        response = self.client.get(f"/files/{scan.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), PDF_BYTES)
        self.assertEqual(FileView.objects.filter(file=scan, user=self.boss).count(), 1)

    def test_replace_shows_versions(self):
        self.post(name="первый.pdf")
        self.post(content=PDF_BYTES + b"2", name="второй.pdf")
        card = self.card()
        self.assertContains(card, "второй.pdf")
        self.assertContains(card, "Версии (2)")
        page = self.client.get(self.scan_url())
        self.assertContains(page, "Версии скана")
        self.assertContains(page, "первый.pdf")
        self.assertContains(page, "второй.pdf")
        self.assertContains(page, "Заменить скан")
        self.assertContains(page, "прежний останется")
        self.assertContains(page, "текущий", count=1)

    def test_rejected_file_shows_an_error(self):
        for content, name, text in ((JPEG_BYTES, "фото.jpg", "только PDF"),
                                    (b"MZ\x90\x00\x03", "скан.pdf", "только PDF"),
                                    # Пустой файл останавливает сама форма Django, до хранилища.
                                    (b"", "пустой.pdf", "файл пуст")):
            page = self.post(content, name)
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, text)
            self.assertContains(page, "Исправьте ошибки")
        big = self.post(PDF_BYTES + b"0" * (5 * MB), "большой.pdf")
        self.assertContains(big, "больше 5 МБ")
        self.assertEqual(self.client.post(self.scan_url(), {}).status_code, 200)  # без файла
        self.branch_agreement.refresh_from_db()
        self.assertIsNone(self.branch_agreement.scan)
        self.assertFalse(StoredFile.objects.exists())

    def test_request_far_over_the_limit_is_refused_before_parsing(self):
        response = self.client.post(self.scan_url(), data=b"0" * (21 * MB), content_type="application/octet-stream")
        self.assertEqual(response.status_code, 413)
        self.assertIn("Слишком большой запрос", response.content.decode())
        self.assertFalse(StoredFile.objects.exists())

    def test_terminated_agreement_page_and_upload(self):
        services.terminate(self.branch_agreement, last_day=D(2022, 1, 1), reason="Истёк срок", by=self.director)
        self.assertEqual(self.client.get(self.scan_url()).status_code, 200)
        self.assertEqual(self.post().status_code, 302)

    # --- кому доступно ---

    def test_other_agent_is_not_found(self):
        # Агент головного офиса филиалу не виден: страница как у несуществующей.
        self.assertEqual(self.client.get(self.scan_url(self.head_agreement)).status_code, 404)
        self.assertEqual(self.post(agreement=self.head_agreement).status_code, 404)
        self.assertFalse(StoredFile.objects.exists())

    def test_agreement_of_another_agent_is_not_found(self):
        # Номер соглашения чужого агента, подставленный в адрес своего.
        url = f"{AGENTS_URL}{self.at_branch.pk}/agreements/{self.head_agreement.pk}/scan/"
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_platform_administrator_is_refused(self):
        platform = User.objects.create_user("platform", kind=User.Kind.PLATFORM)
        give_roles(platform, "Администратор платформы")
        self.client.force_login(platform)
        with refusal_expected(self):
            self.assertEqual(self.client.get(self.scan_url()).status_code, 403)
        with refusal_expected(self):
            self.assertEqual(self.post().status_code, 403)
        self.assertFalse(StoredFile.objects.exists())

    def test_employee_without_agent_rights_is_refused(self):
        plain = self.employee("plain", self.branch, Visibility.COMPANY, roles=("Сотрудник",))
        self.client.force_login(plain)
        with refusal_expected(self):
            self.assertEqual(self.client.get(self.scan_url()).status_code, 403)
        with refusal_expected(self):
            self.assertEqual(self.post().status_code, 403)

    def test_anonymous_goes_to_login(self):
        self.client.logout()
        response = self.client.get(self.scan_url())
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_whole_company_administrator_uploads_for_any_agent(self):
        admin = self.employee("admin", self.branch, Visibility.COMPANY, roles=("Администратор страховой",))
        self.client.force_login(admin)
        self.assertEqual(self.post(agreement=self.head_agreement).status_code, 302)
        self.head_agreement.refresh_from_db()
        self.assertEqual(self.head_agreement.scan.uploaded_by, admin)
        # А соглашение чужой страховой - нет.
        self.assertEqual(self.client.get(self.scan_url(self.foreign_agreement)).status_code, 404)

    def test_scan_is_visible_to_the_same_people_as_the_agent(self):
        scan = self.attach(by=self.boss)
        clerk = self.employee("clerk", self.samarkand, Visibility.DEPARTMENT)
        self.client.force_login(clerk)
        with self.subTest("другое подразделение"):
            self.assertEqual(self.client.get(f"/files/{scan.pk}/").status_code, 404)
        branch_clerk = self.employee("clerk2", self.branch, Visibility.DEPARTMENT)
        self.client.force_login(branch_clerk)
        with self.subTest("то же подразделение"):
            self.assertEqual(self.client.get(f"/files/{scan.pk}/").status_code, 200)
