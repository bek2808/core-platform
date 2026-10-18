"""
Автотесты справочников ЕРСП: загрузка из файлов, повторная загрузка,
пропавшие записи, ошибки в ответе ЕРСП, админ-панель только для чтения.

Запуск на сервере: bin/test references
"""
import copy
import datetime

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from accounts.models import User

from .loader import (
    DATA_DIR,
    LoadError,
    ersp_result,
    load_from_files,
    read_ersp_response,
    read_soato_regions,
    sync_all,
)
from .models import ErspInsurer, ErspOwnershipForm, ErspRegion

ERSP_DIR = DATA_DIR / "ersp"


def file_rows():
    """Строки трёх справочников из файлов в образе (каждый раз свежая копия)."""
    return {
        "regions": read_ersp_response(ERSP_DIR / "regions.json"),
        "ownership_forms": read_ersp_response(ERSP_DIR / "ownership_forms.json"),
        "insurers": read_ersp_response(ERSP_DIR / "insurers.json"),
        "soato_regions": read_soato_regions(),
    }


class DataFilesTests(SimpleTestCase):
    """Файлы справочников в репозитории целы и согласованы между собой."""

    def test_files_have_expected_sizes(self):
        rows = file_rows()
        self.assertEqual(len(rows["regions"]), 14)
        self.assertEqual(len(rows["ownership_forms"]), 24)
        self.assertEqual(len(rows["insurers"]), 49)

    def test_every_region_has_its_own_soato_code(self):
        # Наша таблица СОАТО покрывает ровно регионы ЕРСП, коды не повторяются.
        rows = file_rows()
        region_ids = {row["id"] for row in rows["regions"]}
        soato = rows["soato_regions"]
        self.assertEqual(set(soato), region_ids)
        self.assertEqual(len(set(soato.values())), len(soato))
        for code in soato.values():
            self.assertRegex(code, r"^17\d{2}$")  # 17 - Узбекистан

    def test_tashkent_city_soato_matches_provider_district_codes(self):
        # У провайдера районы Ташкента - 1726277, 1726290: область 1726 = регион 10.
        self.assertEqual(read_soato_regions()[10], "1726")


class LoadFromFilesTests(TestCase):
    """Первая загрузка из файлов."""

    def setUp(self):
        self.results = load_from_files()

    def test_counts(self):
        self.assertEqual(ErspRegion.objects.count(), 14)
        self.assertEqual(ErspOwnershipForm.objects.count(), 24)
        self.assertEqual(ErspInsurer.objects.count(), 49)
        self.assertEqual(self.results["страховщики"].created, 49)

    def test_ersp_id_is_primary_key(self):
        # В ЕРСП уходит именно этот id, поэтому он хранится как есть.
        self.assertEqual(ErspRegion.objects.get(pk=10).name, "Город Ташкент")
        self.assertEqual(ErspOwnershipForm.objects.get(pk=120).name, "Акционерное общество")

    def test_mosaic(self):
        mosaic = ErspInsurer.objects.get(pk=1084)
        self.assertEqual(mosaic.tin, "308143734")
        self.assertEqual(mosaic.industry, ErspInsurer.Industry.GENERAL)
        self.assertIsNone(mosaic.license_stopped_date)
        # Всё, что не вынесено в колонки (серии, рейтинг, разрешения), лежит в raw.
        self.assertEqual(mosaic.raw["series"], "AA")

    def test_empty_tin_stored_as_null(self):
        # У 12 страховщиков ИНН в ЕРСП пустой: NULL не мешает уникальности.
        self.assertEqual(ErspInsurer.objects.filter(tin__isnull=True).count(), 12)

    def test_license_stopped_date_parsed(self):
        self.assertEqual(
            ErspInsurer.objects.get(pk=1082).license_stopped_date,
            datetime.date(2026, 5, 9),
        )

    def test_regions_get_soato_codes(self):
        self.assertFalse(ErspRegion.objects.filter(soato_code__isnull=True).exists())

    def test_names_kept_as_received(self):
        # Название не «причёсываем»: в ЕРСП оно с двумя пробелами - так и храним.
        self.assertEqual(ErspInsurer.objects.get(pk=1076).name, '"INSON"  AJ ST')


class ReloadTests(TestCase):
    """Повторные загрузки: изменения, пропавшие записи, ошибки."""

    def setUp(self):
        self.rows = file_rows()
        sync_all(**copy.deepcopy(self.rows))

    def test_second_load_changes_nothing(self):
        results = sync_all(**copy.deepcopy(self.rows))
        for result in results.values():
            self.assertEqual((result.created, result.updated, result.hidden), (0, 0, 0))
        self.assertEqual(results["страховщики"].unchanged, 49)

    def test_changed_row_is_updated(self):
        rows = copy.deepcopy(self.rows)
        insurer = next(row for row in rows["insurers"] if row["id"] == 1084)
        insurer["licenseStoppedDate"] = "2027-01-15"
        results = sync_all(**rows)
        self.assertEqual(results["страховщики"].updated, 1)
        mosaic = ErspInsurer.objects.get(pk=1084)
        self.assertEqual(mosaic.license_stopped_date, datetime.date(2027, 1, 15))
        self.assertEqual(mosaic.raw["licenseStoppedDate"], "2027-01-15")

    def test_missing_row_is_marked_not_deleted(self):
        rows = copy.deepcopy(self.rows)
        rows["regions"] = [row for row in rows["regions"] if row["id"] != 23]
        results = sync_all(**rows)
        self.assertEqual(results["регионы"].hidden, 1)
        region = ErspRegion.objects.get(pk=23)  # запись осталась
        self.assertFalse(region.is_present)

        # Вернулась в ЕРСП - снова «есть в ЕРСП».
        results = sync_all(**copy.deepcopy(self.rows))
        self.assertEqual(results["регионы"].updated, 1)
        region.refresh_from_db()
        self.assertTrue(region.is_present)

    def test_new_row_is_created(self):
        rows = copy.deepcopy(self.rows)
        rows["ownership_forms"].append({"id": 999, "name": "Новая форма"})
        results = sync_all(**rows)
        self.assertEqual(results["формы собственности"].created, 1)
        self.assertEqual(ErspOwnershipForm.objects.count(), 25)

    def test_error_in_one_file_changes_nothing(self):
        # Регионы правильные, а в страховщиках повтор id: не меняется ни один справочник.
        rows = copy.deepcopy(self.rows)
        rows["regions"][0]["name"] = "Изменённое название"
        rows["insurers"].append(copy.deepcopy(rows["insurers"][0]))
        with self.assertRaisesMessage(LoadError, "повторяется"):
            sync_all(**rows)
        self.assertNotEqual(
            ErspRegion.objects.get(pk=rows["regions"][0]["id"]).name, "Изменённое название"
        )

    def test_repeated_insurer_tin_is_clear_error(self):
        rows = copy.deepcopy(self.rows)
        rows["insurers"][1]["tin"] = rows["insurers"][2]["tin"]
        with self.assertRaisesMessage(LoadError, "ИНН повторяется"):
            sync_all(**rows)

    def test_row_without_name_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows["regions"][0]["name"] = "  "
        with self.assertRaisesMessage(LoadError, "нет поля name"):
            sync_all(**rows)


class ErspResponseTests(SimpleTestCase):
    """Разбор ответа ЕРСП."""

    def test_error_response_rejected(self):
        with self.assertRaisesMessage(LoadError, "Неверный токен"):
            ersp_result({"error": 1, "error_message": "Неверный токен", "result": None})

    def test_response_without_list_rejected(self):
        with self.assertRaisesMessage(LoadError, "нет списка result"):
            ersp_result({"error": 0, "error_message": "", "result": {}})


class CommandTests(TestCase):
    def test_command_loads_and_reports(self):
        from io import StringIO

        out = StringIO()
        call_command("load_ersp_references", stdout=out)
        self.assertIn("страховщики: новых 49", out.getvalue())
        self.assertEqual(ErspInsurer.objects.count(), 49)


class AdminReadOnlyTests(TestCase):
    """Справочники ЕРСП в админ-панели видны, но не редактируются никем."""

    def setUp(self):
        load_from_files()
        admin = User.objects.create_superuser("admin", password="Kx7#mQ2v!pLr9zT")
        self.client.force_login(admin)

    def test_list_and_view_pages_open(self):
        self.assertEqual(self.client.get("/admin/references/erspinsurer/").status_code, 200)
        page = self.client.get("/admin/references/erspinsurer/1084/change/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "308143734")

    def test_superuser_cannot_add_or_delete(self):
        self.assertEqual(self.client.get("/admin/references/erspregion/add/").status_code, 403)
        self.assertEqual(
            self.client.post("/admin/references/erspregion/10/delete/", {"post": "yes"}).status_code,
            403,
        )
        self.assertTrue(ErspRegion.objects.filter(pk=10).exists())

    def test_superuser_cannot_change(self):
        self.client.post("/admin/references/erspregion/10/change/", {"name": "Взлом"})
        self.assertEqual(ErspRegion.objects.get(pk=10).name, "Город Ташкент")
