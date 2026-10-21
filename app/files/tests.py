"""
Автотесты хранилища файлов (часть F3a; модель: docs/files-model.md).

Главное, что проверяется:
- загрузка: тип определяется по содержимому (переименованный «вирус.exe» не
  проходит), пустой файл и файл больше предела отклоняются, на диске при
  отказе ничего не остаётся; скан соглашения - только PDF до 5 МБ;
- файл лежит под случайным именем в папке своей страховой, исходное имя
  хранится в базе и очищается от пути и управляющих знаков;
- сохранение в базу не удалось - файл с диска убирается;
- записи только пополняются: изменить и удалить нельзя ни из приложения,
  ни в обход него (триггеры в базе), ограничения таблицы держит база;
- files_check находит пропавший, испорченный и лишний файл;
- журнал просмотров защищён так же.

Скачивание и видимость - files/test_download.py.
Запуск на сервере: bin/test files
"""
import hashlib
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase, override_settings

from agents.apps import AGREEMENT_SCAN
from companies.models import InsuranceCompany

from . import registry, storage
from .models import AppendOnlyError, FileView, StoredFile
from .storage import FileRejected

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 100
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

# Назначение только для тестов: принимает и фото, предел 2 КБ (чтобы не гонять мегабайты).
TEST_PURPOSE = "test_photo"
registry.register(registry.Purpose(
    code=TEST_PURPOSE, label="тестовое фото",
    types=frozenset({registry.PDF, registry.JPEG, registry.PNG}), max_size=2048,
    owners=lambda scope: InsuranceCompany.objects.none(), view_perm="agents.view_agreement",
))


def upload(content=PDF_BYTES, name="Соглашение №5.pdf"):
    return SimpleUploadedFile(name, content)


class StorageCase(TestCase):
    """Каждому тесту - своя временная папка хранилища (база после теста откатывается, файлы - нет)."""

    @classmethod
    def setUpTestData(cls):
        cls.company = InsuranceCompany.objects.create(name="Mosaic Insurance Group", code="mosaic")

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.enterContext(override_settings(FILES_ROOT=Path(folder.name)))
        self.root = storage.root()

    def put(self, content=PDF_BYTES, name="Соглашение №5.pdf", purpose=AGREEMENT_SCAN, owner_id=1):
        return storage.store(upload(content, name), purpose=purpose, owner_id=owner_id, company=self.company)

    def files_on_disk(self):
        return sorted(
            str(path.relative_to(self.root)) for path in self.root.rglob("*")
            if path.is_file() and path.relative_to(self.root).parts[0] != storage.TMP_DIR
        )

    def temp_files(self):
        folder = self.root / storage.TMP_DIR
        return list(folder.iterdir()) if folder.exists() else []


class StoreTests(StorageCase):
    def test_pdf_stored_with_hash_and_random_name(self):
        record = self.put()
        self.assertEqual(record.content_type, "application/pdf")
        self.assertEqual(record.size, len(PDF_BYTES))
        self.assertEqual(record.sha256, hashlib.sha256(PDF_BYTES).hexdigest())
        self.assertEqual(record.original_name, "Соглашение №5.pdf")
        self.assertEqual((self.root / record.path).read_bytes(), PDF_BYTES)
        # Папка своей страховой, имя на диске - случайное, не имя человека.
        self.assertTrue(record.path.startswith(f"{self.company.pk}/"))
        self.assertTrue(record.path.endswith(".pdf"))
        self.assertNotIn("Соглашение", record.path)
        self.assertEqual(self.files_on_disk(), [record.path])
        self.assertEqual(self.temp_files(), [])

    def test_two_uploads_two_files(self):
        first, second = self.put(), self.put()
        self.assertNotEqual(first.path, second.path)
        self.assertNotEqual(first.pk, second.pk)

    def test_type_by_content_not_by_name(self):
        # Исполняемый файл, переименованный в PDF.
        with self.assertRaises(FileRejected) as caught:
            self.put(b"MZ\x90\x00\x03\x00\x00\x00", name="скан.pdf")
        self.assertIn("только PDF", str(caught.exception))
        # И наоборот: настоящий PDF с чужим окончанием принимается как PDF.
        record = self.put(name="скан.jpg")
        self.assertEqual(record.content_type, "application/pdf")
        self.assertTrue(record.path.endswith(".pdf"))
        self.assertEqual(StoredFile.objects.count(), 1)

    def test_rejected_leaves_nothing_behind(self):
        for content in (b"", b"just text", b"%PDF"[:3]):
            with self.assertRaises(FileRejected):
                self.put(content)
        self.assertEqual(StoredFile.objects.count(), 0)
        self.assertEqual(self.files_on_disk(), [])
        self.assertEqual(self.temp_files(), [])

    def test_empty_file(self):
        with self.assertRaises(FileRejected) as caught:
            self.put(b"")
        self.assertIn("пустой", str(caught.exception))

    def test_agreement_scan_is_pdf_only(self):
        for content in (JPEG_BYTES, PNG_BYTES):
            with self.assertRaises(FileRejected) as caught:
                self.put(content, name="фото.jpg")
            self.assertIn("только PDF", str(caught.exception))
        self.assertEqual(self.files_on_disk(), [])

    def test_other_purpose_takes_photos(self):
        jpeg = self.put(JPEG_BYTES, name="IMG_1.jpg", purpose=TEST_PURPOSE)
        png = self.put(PNG_BYTES, name="screen.png", purpose=TEST_PURPOSE)
        self.assertEqual((jpeg.content_type, png.content_type), ("image/jpeg", "image/png"))
        self.assertTrue(jpeg.path.endswith(".jpg"))
        self.assertTrue(png.path.endswith(".png"))

    def test_size_limit_of_agreement_scan_is_5_mb(self):
        limit = 5 * registry.MB
        exact = PDF_BYTES + b"0" * (limit - len(PDF_BYTES))
        self.assertEqual(len(exact), limit)
        self.assertEqual(self.put(exact).size, limit)
        with self.assertRaises(FileRejected) as caught:
            self.put(exact + b"0")
        self.assertIn("больше 5 МБ", str(caught.exception))
        self.assertEqual(StoredFile.objects.count(), 1)
        self.assertEqual(self.temp_files(), [])

    def test_upload_stops_reading_at_the_limit(self):
        read = []

        class Endless:
            name = "big.pdf"

            def chunks(self, size):
                yield PDF_BYTES
                while True:
                    read.append(1)
                    yield b"0" * size

        with self.assertRaises(FileRejected):
            storage.store(Endless(), purpose=TEST_PURPOSE, owner_id=1, company=self.company)
        self.assertLess(len(read), 10)  # предел 2 КБ: оборвали сразу, а не читали бесконечно

    def test_name_is_cleaned(self):
        self.assertEqual(self.put(name="../../etc/passwd.pdf").original_name, "passwd.pdf")
        self.assertEqual(self.put(name="C:\\Users\\bek\\скан.pdf").original_name, "скан.pdf")
        self.assertEqual(self.put(name="a\x00b\nc.pdf").original_name, "abc.pdf")
        # Имя из одних точек и косых черт после очистки пустое. Браузер файл без имени
        # не присылает, поэтому вместо UploadedFile - простой заменитель.
        class Nameless:
            name = "../.."

            def chunks(self, size):
                yield PDF_BYTES

        nameless = storage.store(Nameless(), purpose=AGREEMENT_SCAN, owner_id=1, company=self.company)
        self.assertEqual(nameless.original_name, "file.pdf")
        long_name = self.put(name="я" * 300 + ".pdf").original_name
        self.assertEqual(len(long_name), 255)
        self.assertTrue(long_name.endswith(".pdf"))

    def test_database_failure_removes_the_file(self):
        with mock.patch.object(StoredFile, "save", side_effect=RuntimeError("база недоступна")):
            with self.assertRaises(RuntimeError):
                self.put()
        self.assertEqual(self.files_on_disk(), [])
        self.assertEqual(self.temp_files(), [])

    def test_open_path_stays_inside_storage(self):
        record = self.put()
        self.assertEqual(storage.open_path(record), (self.root / record.path).resolve())
        outside = StoredFile(path="../outside.pdf")
        with self.assertRaises(FileNotFoundError):
            storage.open_path(outside)
        with self.assertRaises(FileNotFoundError):
            storage.open_path(StoredFile(path=f"{self.company.pk}/нет/такого.pdf"))


class AppendOnlyTests(StorageCase):
    """Файл не меняется и не удаляется: сначала запрет в приложении, потом триггер в базе."""

    def setUp(self):
        super().setUp()
        self.record = self.put()
        self.view = FileView.objects.create(file=self.record)

    def refused_by_database(self, action):
        with self.assertRaises(DatabaseError), transaction.atomic():
            action()

    def test_application_refuses_changes(self):
        self.record.original_name = "другое.pdf"
        with self.assertRaises(AppendOnlyError):
            self.record.save()
        with self.assertRaises(AppendOnlyError):
            self.record.delete()
        with self.assertRaises(AppendOnlyError):
            StoredFile.objects.filter(pk=self.record.pk).update(original_name="x")
        with self.assertRaises(AppendOnlyError):
            StoredFile.objects.all().delete()
        with self.assertRaises(AppendOnlyError):
            FileView.objects.all().delete()
        with self.assertRaises(AppendOnlyError):
            self.view.save()

    def test_database_refuses_update_delete_truncate(self):
        pk = self.record.pk
        self.refused_by_database(lambda: StoredFile._base_manager.filter(pk=pk).update(original_name="x"))
        self.refused_by_database(lambda: StoredFile._base_manager.filter(pk=pk).delete())
        self.refused_by_database(lambda: FileView._base_manager.filter(pk=self.view.pk).delete())
        self.refused_by_database(lambda: FileView._base_manager.filter(pk=self.view.pk).update(user=None))
        for table in ("files_storedfile", "files_fileview"):
            self.refused_by_database(lambda table=table: connection.cursor().execute(f"TRUNCATE {table} CASCADE"))
        self.record.refresh_from_db()
        self.assertEqual(self.record.original_name, "Соглашение №5.pdf")

    def test_table_rules_held_by_the_database(self):
        def broken(**fields):
            data = dict(
                insurance_company=self.company, purpose=AGREEMENT_SCAN, owner_id=1, original_name="x.pdf",
                content_type="application/pdf", size=10, sha256="a" * 64, path="x/y/z.pdf",
            )
            data.update(fields)
            return StoredFile(**data).save()

        for fields in ({"size": 0}, {"content_type": "text/html"}, {"sha256": "Z" * 64},
                       {"sha256": "a" * 63}, {"path": self.record.path}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                broken(**fields)
        broken()  # а правильная запись проходит


class FilesCheckTests(StorageCase):
    def check(self):
        out = StringIO()
        try:
            call_command("files_check", stdout=out)
        except CommandError as error:
            return out.getvalue(), str(error)
        return out.getvalue(), None

    def test_all_fine(self):
        self.put()
        self.put(JPEG_BYTES, name="a.jpg", purpose=TEST_PURPOSE)
        output, error = self.check()
        self.assertIsNone(error)
        self.assertIn("Записей о файлах: 2", output)
        self.assertIn("OK", output)

    def test_empty_storage_is_fine(self):
        output, error = self.check()
        self.assertIsNone(error)
        self.assertIn("Записей о файлах: 0", output)

    def test_missing_file_is_an_error(self):
        record = self.put()
        (self.root / record.path).unlink()
        output, error = self.check()
        self.assertIn("НЕТ ФАЙЛА", output)
        self.assertIn(str(record.pk), output)
        self.assertIn("пропало 1", error)

    def test_damaged_file_is_an_error(self):
        record = self.put()
        path = self.root / record.path
        path.write_bytes(path.read_bytes().replace(b"Catalog", b"Katalog"))  # тот же размер, другое содержимое
        output, error = self.check()
        self.assertIn("НЕ СОВПАДАЕТ ОТПЕЧАТОК", output)
        self.assertIn("испорчено 1", error)

    def test_truncated_file_is_an_error(self):
        record = self.put()
        path = self.root / record.path
        path.write_bytes(path.read_bytes()[:-5])
        output, error = self.check()
        self.assertIn("НЕ СОВПАДАЕТ ОТПЕЧАТОК", output)
        self.assertIsNotNone(error)

    def test_extra_file_is_only_a_warning(self):
        self.put()
        stray = self.root / str(self.company.pk) / "2026" / "10" / "stray.pdf"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(PDF_BYTES)
        output, error = self.check()
        self.assertIsNone(error)
        self.assertIn("ЛИШНИЙ ФАЙЛ", output)
        self.assertIn("stray.pdf", output)
        self.assertIn("Лишних файлов: 1", output)
        self.assertTrue(stray.exists())  # ничего не удаляет

    def test_temporary_folder_is_not_an_extra_file(self):
        self.put()
        (self.root / storage.TMP_DIR).mkdir(exist_ok=True)
        (self.root / storage.TMP_DIR / "half-written.part").write_bytes(b"%PDF")
        output, error = self.check()
        self.assertIsNone(error)
        self.assertNotIn("ЛИШНИЙ", output)
