"""
Автотесты перечня банков ЦБ (references/cbu.py): поиск файла на странице ЦБ,
разбор листа «Русский», проверки строк, загрузка в CbuBank, экран просмотра.

В сеть тесты не ходят: файл Excel собирается в тесте (cbu_xlsx) так же,
как устроен настоящий файл ЦБ (проверен 21.10.2026), а скачивание
подменяется заглушкой.

Запуск на сервере: bin/test references
"""
import datetime
import io

from django.contrib.auth.models import Group
from django.test import SimpleTestCase, TestCase
from openpyxl import Workbook

from accounts.models import User
from config.testing import refusal_expected

from . import cbu
from .loader import LoadError
from .models import CbuBank

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"

HEADER = [
    "Код филиала", "Признак филиала", "Наименование банка", "Наименование филиала", "Адрес филиала",
    "Дата открытия", "Дата изменения", "Область", "Район", "Город", "ИНН", "Веб-сайт", "Геолокация",
]

# Строки как в настоящем файле ЦБ: МФО строкой с ведущими нулями, даты - числами Excel.
ALOQABANK = {"Код филиала": "00401", "Признак филиала": 1, "Наименование банка": "ALOQABANK",
             "Наименование филиала": 'AT "ALOQABANK" BOSH OFISI', "Адрес филиала": "100000, город Ташкент",
             "Дата открытия": 35494, "Дата изменения": 46189, "Область": "город Ташкент", "ИНН": "200829053"}
ASIA_ALLIANCE = {"Код филиала": "01095", "Признак филиала": 1, "Наименование банка": "ASIA ALLIANCE BANK",
                 "Наименование филиала": '"ASIA ALLIANCE BANK" AT BANKI', "Дата изменения": 45000,
                 "ИНН": "207018693"}
KAPITALBANK = {"Код филиала": "01158", "Признак филиала": 3, "Наименование банка": "KAPITALBANK",
               "Наименование филиала": '"KAPITALBANK" AT BANKINING YAGONA FILIALI', "ИНН": "207275139"}
PAYMENT_CENTER = {"Код филиала": "09001", "Признак филиала": 2, "Наименование банка": "UZUM BANK",
                  "Наименование филиала": "UZUM BANK TO'LOV MARKAZI"}
ROWS = [ALOQABANK, ASIA_ALLIANCE, KAPITALBANK, PAYMENT_CENTER]


def cbu_xlsx(rows=ROWS, header=HEADER, sheet="Русский"):
    """Файл как у ЦБ: четыре листа, вверху легенда, заголовок в 8-й строке."""
    book = Workbook()
    book.remove(book.active)
    for name in ("O'zbekcha", "Ўзбекча", sheet, "English"):
        ws = book.create_sheet(name)
        if name != sheet:
            ws.append(["Fayl yaratilgan sana"])
            continue
        ws.append(["Дата создания файла"])
        ws.append(["18.11.2024", "Признак филиала"])
        ws.append([None, "0 - структурные подразделения Центрального банка"])
        ws.append(["Количество строк", "1 - головные офисы коммерческих банков"])
        ws.append([376, "2 - платежные центры коммерческих банков"])
        ws.append([None, "3 - филиалы коммерческих банков"])
        ws.append([])
        ws.append(header)
        for row in rows:
            ws.append([row.get(title) for title in header])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


PAGE_HTML = (
    '<a href="/ru/">Главная</a> <a class="file" '
    'href="/upload/iblock/285/abc123/Filiallar_haqida_malumot.xlsx">xlsx 139.0 КБ</a>'
)


class FakeFetch:
    """Заглушка сайта ЦБ: адрес -> байты; запоминает, что запрашивали."""

    def __init__(self, content=None, html=PAGE_HTML):
        self.pages = {cbu.PAGE_URL: html.encode("utf-8")}
        self.content = content if content is not None else cbu_xlsx()
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url in self.pages:
            return self.pages[url]
        return self.content


class ReadTests(SimpleTestCase):
    def test_rows_parsed(self):
        rows = cbu.read_rows(cbu_xlsx())
        self.assertEqual([r["mfo"] for r in rows], ["00401", "01095", "01158", "09001"])
        aloqa = rows[0]
        self.assertEqual(aloqa["kind"], 1)
        self.assertEqual(aloqa["inn"], "200829053")
        self.assertEqual(aloqa["branch_name"], 'AT "ALOQABANK" BOSH OFISI')
        self.assertEqual(aloqa["bank_name"], "ALOQABANK")
        self.assertEqual(aloqa["opened_on"], datetime.date(1997, 3, 5))
        self.assertEqual(aloqa["changed_on"], datetime.date(2026, 6, 16))
        self.assertEqual(aloqa["raw"]["ИНН"], "200829053")
        self.assertEqual(rows[3]["inn"], "")  # у платёжных центров ИНН нет

    def test_columns_found_by_title(self):
        shuffled = list(reversed(HEADER))
        rows = cbu.read_rows(cbu_xlsx(header=shuffled))
        self.assertEqual(rows[0]["mfo"], "00401")
        self.assertEqual(rows[0]["inn"], "200829053")

    def test_optional_columns_may_be_missing(self):
        rows = cbu.read_rows(cbu_xlsx(header=[t for t in HEADER if t not in ("Адрес филиала", "Дата открытия")]))
        self.assertEqual(rows[0]["address"], "")
        self.assertIsNone(rows[0]["opened_on"])

    def test_numeric_mfo_gets_leading_zeros(self):
        rows = cbu.read_rows(cbu_xlsx([{**ALOQABANK, "Код филиала": 401}]))
        self.assertEqual(rows[0]["mfo"], "00401")

    def test_missing_required_column(self):
        with self.assertRaisesMessage(LoadError, "нет колонок: ИНН"):
            cbu.read_rows(cbu_xlsx(header=[t for t in HEADER if t != "ИНН"]))

    def test_no_header(self):
        with self.assertRaisesMessage(LoadError, "не найдена строка заголовков"):
            cbu.read_rows(cbu_xlsx(header=["Код", "ИНН"]))

    def test_no_russian_sheet(self):
        with self.assertRaisesMessage(LoadError, "нет листа «Русский»"):
            cbu.read_rows(cbu_xlsx(sheet="Ruscha"))

    def test_not_excel(self):
        with self.assertRaisesMessage(LoadError, "не читается как Excel"):
            cbu.read_rows(b"<html>Bad Gateway</html>")

    def test_no_rows(self):
        with self.assertRaisesMessage(LoadError, "нет ни одной строки"):
            cbu.read_rows(cbu_xlsx(rows=[]))

    def test_bad_values_refused(self):
        cases = [
            ({**ALOQABANK, "Код филиала": "4O1"}, "не 5 цифр"),
            ({**ALOQABANK, "ИНН": "20082905"}, "не 9 цифр"),
            ({**ALOQABANK, "Признак филиала": "головной"}, "не число"),
            ({**ALOQABANK, "Наименование филиала": ""}, "нет наименования"),
            ({**ALOQABANK, "Дата изменения": "вчера"}, "непонятная дата"),
        ]
        for row, message in cases:
            with self.subTest(message=message), self.assertRaisesMessage(LoadError, message):
                cbu.read_rows(cbu_xlsx([row]))


class FindFileTests(SimpleTestCase):
    def test_relative_link_resolved(self):
        self.assertEqual(
            cbu.find_file_url(PAGE_HTML),
            "https://cbu.uz/upload/iblock/285/abc123/Filiallar_haqida_malumot.xlsx",
        )

    def test_no_link(self):
        with self.assertRaisesMessage(LoadError, "нет ссылки на файл .xlsx"):
            cbu.find_file_url('<a href="/ru/">Главная</a>')

    def test_several_links(self):
        with self.assertRaisesMessage(LoadError, "несколько файлов"):
            cbu.find_file_url('<a href="/a.xlsx">1</a><a href="/b.xlsx">2</a>')

    def test_foreign_host_refused(self):
        with self.assertRaisesMessage(LoadError, "не на cbu.uz"):
            cbu.find_file_url('<a href="https://evil.example/banks.xlsx">1</a>')

    def test_download_uses_link_from_page(self):
        fake = FakeFetch()
        url, content = cbu.download(fake)
        self.assertEqual(fake.calls, [cbu.PAGE_URL, url])
        self.assertTrue(url.endswith("Filiallar_haqida_malumot.xlsx"))
        self.assertEqual(content, fake.content)

    def test_only_https(self):
        with self.assertRaisesMessage(LoadError, "только по https"):
            cbu.urllib_fetch("http://cbu.uz/")


class SyncTests(TestCase):
    def test_created_then_unchanged(self):
        rows = cbu.read_rows(cbu_xlsx())
        first = cbu.sync(rows)
        self.assertEqual((first.created, first.updated, first.unchanged, first.hidden), (4, 0, 0, 0))
        second = cbu.sync(cbu.read_rows(cbu_xlsx()))
        self.assertEqual((second.created, second.updated, second.unchanged, second.hidden), (0, 0, 4, 0))
        aloqa = CbuBank.objects.get(mfo="00401")
        self.assertEqual((aloqa.inn, aloqa.kind, aloqa.changed_on), ("200829053", 1, datetime.date(2026, 6, 16)))
        self.assertEqual(aloqa.raw["Наименование банка"], "ALOQABANK")

    def test_changed_and_gone(self):
        cbu.sync(cbu.read_rows(cbu_xlsx()))
        renamed = {**KAPITALBANK, "Наименование филиала": "KAPITALBANK FILIALI"}
        result = cbu.sync(cbu.read_rows(cbu_xlsx([ALOQABANK, ASIA_ALLIANCE, renamed])))
        self.assertEqual((result.updated, result.hidden), (1, 1))
        self.assertEqual(CbuBank.objects.get(mfo="01158").branch_name, "KAPITALBANK FILIALI")
        gone = CbuBank.objects.get(mfo="09001")
        self.assertFalse(gone.is_present)  # не удалён, а помечен
        back = cbu.sync(cbu.read_rows(cbu_xlsx()))
        self.assertEqual(back.updated, 2)
        self.assertTrue(CbuBank.objects.get(mfo="09001").is_present)

    def test_repeated_mfo_refused(self):
        with self.assertRaisesMessage(LoadError, "МФО 00401 повторяется"):
            cbu.sync(cbu.read_rows(cbu_xlsx([ALOQABANK, ALOQABANK])))
        self.assertFalse(CbuBank.objects.exists())


class AdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cbu.sync(cbu.read_rows(cbu_xlsx()))

    def test_employee_may_view(self):
        user = User.objects.create_user("ivanov", password=STRONG_PASSWORD, is_staff=True)
        user.groups.add(Group.objects.get(name="Сотрудник"))
        self.client.force_login(user)
        response = self.client.get("/admin/references/cbubank/")
        self.assertContains(response, "200829053")
        self.assertContains(response, "ALOQABANK")

    def test_read_only_even_for_superuser(self):
        self.client.force_login(User.objects.create_superuser("root", password=STRONG_PASSWORD))
        self.assertContains(self.client.get("/admin/references/cbubank/00401/change/"), "BOSH OFISI")
        with refusal_expected(self):
            self.assertEqual(self.client.get("/admin/references/cbubank/add/").status_code, 403)
        with refusal_expected(self):
            self.client.post("/admin/references/cbubank/00401/change/", {"inn": "111111111"})
        self.assertEqual(CbuBank.objects.get(mfo="00401").inn, "200829053")
