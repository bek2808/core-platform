"""
Автотесты справочников ЕРСП (регионы, районы, формы собственности,
страховщики, валюты, ОКЭД, типы местности, типы обязательного страхования,
типы резидентства, страны): загрузка из файлов, повторная загрузка,
пропавшие записи, ошибки в ответе ЕРСП, админ-панель только для чтения.

Запуск на сервере: bin/test references
"""
import copy
import datetime

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from accounts.models import User
from config.testing import refusal_expected

from .loader import (
    DATA_DIR,
    LoadError,
    ersp_result,
    load_from_files,
    read_ersp_response,
    read_soato_regions,
    sync_all,
)
from .models import (
    ErspAreaType,
    ErspCountry,
    ErspCurrency,
    ErspDistrict,
    ErspInsuranceType,
    ErspInsurer,
    ErspOked,
    ErspOwnershipForm,
    ErspRegion,
    ErspResidentType,
)

ERSP_DIR = DATA_DIR / "ersp"


def file_rows():
    """Строки справочников из файлов в образе (каждый раз свежая копия)."""
    return {
        "regions": read_ersp_response(ERSP_DIR / "regions.json"),
        "ownership_forms": read_ersp_response(ERSP_DIR / "ownership_forms.json"),
        "insurers": read_ersp_response(ERSP_DIR / "insurers.json"),
        "soato_regions": read_soato_regions(),
        "districts": read_ersp_response(ERSP_DIR / "districts.json"),
        "currencies": read_ersp_response(ERSP_DIR / "currencies.json"),
        "okeds": read_ersp_response(ERSP_DIR / "okeds.json"),
        "area_types": read_ersp_response(ERSP_DIR / "area_types.json"),
        "insurance_types": read_ersp_response(ERSP_DIR / "insurance_types.json"),
        "resident_types": read_ersp_response(ERSP_DIR / "resident_types.json"),
        "countries": read_ersp_response(ERSP_DIR / "countries.json"),
    }


class DataFilesTests(SimpleTestCase):
    """Файлы справочников в репозитории целы и согласованы между собой."""

    def test_files_have_expected_sizes(self):
        rows = file_rows()
        self.assertEqual(len(rows["regions"]), 14)
        self.assertEqual(len(rows["ownership_forms"]), 24)
        self.assertEqual(len(rows["insurers"]), 49)
        self.assertEqual(len(rows["districts"]), 249)
        self.assertEqual(len(rows["currencies"]), 164)
        self.assertEqual(len(rows["okeds"]), 696)
        self.assertEqual(len(rows["area_types"]), 3)
        self.assertEqual(len(rows["insurance_types"]), 19)
        self.assertEqual(len(rows["resident_types"]), 6)
        self.assertEqual(len(rows["countries"]), 247)

    def test_every_district_in_known_region(self):
        rows = file_rows()
        region_ids = {row["id"] for row in rows["regions"]}
        self.assertTrue(all(row["regionId"] in region_ids for row in rows["districts"]))

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

    @classmethod
    def setUpTestData(cls):
        cls.results = load_from_files()  # один раз на группу тестов

    def test_counts(self):
        self.assertEqual(ErspRegion.objects.count(), 14)
        self.assertEqual(ErspOwnershipForm.objects.count(), 24)
        self.assertEqual(ErspInsurer.objects.count(), 49)
        self.assertEqual(self.results["страховщики"].created, 49)
        self.assertEqual(ErspDistrict.objects.count(), 249)
        self.assertEqual(ErspCurrency.objects.count(), 164)
        self.assertEqual(ErspOked.objects.count(), 696)
        self.assertEqual(ErspAreaType.objects.count(), 3)
        self.assertEqual(ErspInsuranceType.objects.count(), 19)
        self.assertEqual(ErspResidentType.objects.count(), 6)
        self.assertEqual(ErspCountry.objects.count(), 247)

    def test_new_references_by_ersp_id(self):
        # Эти id уходят в ЕРСП (areaTypeId, insuranceTypeId, residentType, countryId).
        self.assertEqual(ErspAreaType.objects.get(pk=1).name, "Городская местность")
        self.assertEqual(ErspInsuranceType.objects.get(pk=2).name, "ОСАГО")
        self.assertEqual(ErspInsuranceType.objects.get(pk=999).name, "Другие")
        self.assertEqual(ErspResidentType.objects.get(pk=1).name, "Гражданин")
        self.assertEqual(ErspCountry.objects.get(pk=ErspCountry.UZBEKISTAN).name, "Узбекистан")
        self.assertEqual(ErspCountry.objects.get(pk=9999).name, "Все страны")

    def test_area_type_name_without_line_break(self):
        # У сельской местности в ЕРСП узбекское название с \r в конце: в базе без него, в raw - как пришло.
        rural = ErspAreaType.objects.get(pk=2)
        self.assertEqual(rural.name_uzc, "Қишлоқ ҳудуди")
        self.assertTrue(rural.raw["nameUzc"].endswith("\r"))

    def test_empty_uzbek_names_allowed(self):
        # У части записей nameUz пустой (null) - это не ошибка.
        self.assertEqual(ErspAreaType.objects.get(pk=3).name_uz, "")

    def test_district_belongs_to_region(self):
        # В ЕРСП номер района - не СОАТО: Шайхантахурский - 1006 (СОАТО 1726277).
        district = ErspDistrict.objects.get(pk=1006)
        self.assertEqual(district.region_id, 10)
        self.assertEqual(ErspDistrict.objects.get(pk=739001080).name, "ЯШНАБАДСКИЙ РАЙОН")

    def test_currency_by_iso_numeric_code(self):
        # id валюты в ЕРСП - цифровой код ISO 4217; на него и опираемся.
        self.assertEqual(ErspCurrency.objects.get(pk=ErspCurrency.UZS).ticker, "UZS")
        self.assertEqual(ErspCurrency.objects.get(pk=840).ticker, "USD")

    def test_currency_kept_as_received(self):
        # Ошибки ЕРСП не исправляем: у евро код EVR. Убраны только пробелы по краям.
        self.assertEqual(ErspCurrency.objects.get(pk=978).ticker, "EVR")
        self.assertEqual(ErspCurrency.objects.get(pk=972).ticker, "TJS")
        self.assertEqual(ErspCurrency.objects.get(pk=972).raw["ticker"], "TJS ")

    def test_oked_code_keeps_leading_zero(self):
        self.assertTrue(ErspOked.objects.filter(pk="01452").exists())
        self.assertEqual(
            ErspOked.objects.get(pk="65120").name_uz,
            "Hayotni sug‘urtalashdan tashqari sug‘urtalash",
        )

    def test_oked_upper_level_codes_kept(self):
        # В ЕРСП есть 6 кодов верхних уровней классификатора (2-4 цифры).
        self.assertTrue(ErspOked.objects.filter(pk="9411").exists())
        self.assertTrue(ErspOked.objects.filter(pk="73").exists())

    def test_oked_names_without_line_breaks(self):
        # В ответе ЕРСП у части названий в конце \r - в базе его нет, в raw - есть.
        oked = ErspOked.objects.get(pk="94910")
        self.assertFalse(oked.name.endswith("\r"))
        self.assertTrue(oked.raw["name"].endswith("\r"))

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

    @classmethod
    def setUpTestData(cls):
        sync_all(**file_rows())  # первая загрузка - один раз на группу тестов

    def setUp(self):
        self.rows = file_rows()  # свежая копия строк - тест может их менять

    def test_second_load_changes_nothing(self):
        results = sync_all(**copy.deepcopy(self.rows))
        for result in results.values():
            self.assertEqual((result.created, result.updated, result.hidden), (0, 0, 0))
        self.assertEqual(results["страховщики"].unchanged, 49)
        self.assertEqual(results["ОКЭД"].unchanged, 696)

    def test_district_with_unknown_region_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows["districts"][0]["regionId"] = 99
        with self.assertRaises(LoadError):
            sync_all(**rows)

    def test_wrong_oked_code_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows["okeds"][0]["id"] = "65 1A"
        with self.assertRaises(LoadError):
            sync_all(**rows)

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
        self.assertIn("ОКЭД: новых 696", out.getvalue())
        self.assertIn("страны: новых 247", out.getvalue())
        self.assertEqual(ErspInsurer.objects.count(), 49)


class AdminReadOnlyTests(TestCase):
    """Справочники ЕРСП в админ-панели видны, но не редактируются никем."""

    @classmethod
    def setUpTestData(cls):
        load_from_files()  # один раз на группу тестов
        cls.admin = User.objects.create_superuser("admin", password="Kx7#mQ2v!pLr9zT")

    def setUp(self):
        self.client.force_login(self.admin)

    def test_list_and_view_pages_open(self):
        self.assertEqual(self.client.get("/admin/references/erspinsurer/").status_code, 200)
        page = self.client.get("/admin/references/erspinsurer/1084/change/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "308143734")

    def test_new_reference_pages_open_read_only(self):
        for model in ["erspareatype", "erspinsurancetype", "erspresidenttype", "erspcountry"]:
            self.assertEqual(self.client.get(f"/admin/references/{model}/").status_code, 200, model)
        self.assertContains(self.client.get("/admin/references/erspcountry/210/change/"), "Узбекистан")
        with refusal_expected(self):
            self.assertEqual(self.client.get("/admin/references/erspcountry/add/").status_code, 403)

    def test_superuser_cannot_add_or_delete(self):
        with refusal_expected(self):
            self.assertEqual(self.client.get("/admin/references/erspregion/add/").status_code, 403)
        with refusal_expected(self):
            response = self.client.post("/admin/references/erspregion/10/delete/", {"post": "yes"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ErspRegion.objects.filter(pk=10).exists())

    def test_superuser_cannot_change(self):
        with refusal_expected(self):
            self.client.post("/admin/references/erspregion/10/change/", {"name": "Взлом"})
        self.assertEqual(ErspRegion.objects.get(pk=10).name, "Город Ташкент")


class NewReferencesRightsTests(TestCase):
    """Новые справочники ЕРСП сотрудники только смотрят (миграция references/0006)."""

    def test_groups_can_view_not_change(self):
        from django.contrib.auth.models import Group

        for name in ["Администратор справочников", "Сотрудник"]:
            rights = set(Group.objects.get(name=name).permissions.values_list("codename", flat=True))
            for model in ["erspareatype", "erspinsurancetype", "erspresidenttype", "erspcountry"]:
                self.assertIn(f"view_{model}", rights, name)
                self.assertNotIn(f"change_{model}", rights, name)
