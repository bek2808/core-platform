"""
Перечень банков Центрального банка Узбекистана (cbu.uz) -> таблица CbuBank.

Откуда: страница «Филиалы коммерческих банков»
(https://cbu.uz/ru/credit-organizations/banks/branches/). На ней один файл
Excel со всеми банками: головные офисы, филиалы, платёжные центры,
подразделения ЦБ - около 320 строк, у каждой МФО («Код филиала») и ИНН.
Адрес файла меняется при каждом обновлении (в нём случайная часть), поэтому
ссылка каждый раз ищется на странице заново.

Как устроен файл (проверено 21.10.2026): листы на четырёх языках, берём
«Русский»; вверху листа - легенда (дата создания файла, число строк, коды
«Признака филиала»), ниже - строка заголовков (в ней ячейка «Код филиала»),
дальше данные. Дата создания и число строк в легенде не обновляются
(18.11.2024 и 376, а записей 324 с изменениями до июля 2026) - на них
не опираемся.

Колонки ищутся по заголовку, а не по номеру: если ЦБ переставит колонки,
загрузка не сломается; если пропадёт нужная - остановится с понятной ошибкой.

Загрузка - «всё или ничего» (references.loader.sync_reference в одной
транзакции), повторная с тем же файлом ничего не меняет.
"""
import datetime
import io
import re
import urllib.parse
import urllib.request
import zipfile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from .loader import LoadError, sync_reference
from .models import CbuBank

PAGE_URL = "https://cbu.uz/ru/credit-organizations/banks/branches/"
SHEET = "Русский"
TIMEOUT_S = 30
MAX_BYTES = 10 * 1024 * 1024  # файл ~140 КБ; больше 10 МБ - явно не он

# Заголовок колонки в файле ЦБ -> поле CbuBank. Без звёздочки - необязательная.
COLUMNS = {
    "Код филиала": "mfo",
    "Признак филиала": "kind",
    "Наименование банка": "bank_name",
    "Наименование филиала": "branch_name",
    "ИНН": "inn",
    "Адрес филиала": "address",
    "Область": "region",
    "Район": "district",
    "Город": "city",
    "Дата открытия": "opened_on",
    "Дата изменения": "changed_on",
}
REQUIRED = ("Код филиала", "Признак филиала", "Наименование банка", "Наименование филиала", "ИНН")

# Excel хранит дату числом дней от 30.12.1899.
EXCEL_EPOCH = datetime.date(1899, 12, 30)


# --- Скачивание ---------------------------------------------------------------------

def urllib_fetch(url):
    """Скачать адрес (только https). Возвращает байты; ошибка сети - LoadError."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise LoadError(f"скачиваем только по https: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "core-platform"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            data = response.read(MAX_BYTES + 1)
    except OSError as error:  # нет связи, тайм-аут, HTTP-ошибка, сертификат
        raise LoadError(f"не удалось скачать {url}: {error}") from error
    if len(data) > MAX_BYTES:
        raise LoadError(f"{url}: ответ больше {MAX_BYTES // (1024 * 1024)} МБ - это не перечень банков")
    return data


def find_file_url(html, page_url=PAGE_URL):
    """Адрес файла Excel на странице ЦБ (ссылка на .xlsx; ожидается ровно одна)."""
    links = sorted(set(re.findall(r'href="([^"]+\.xlsx)"', html, flags=re.IGNORECASE)))
    if not links:
        raise LoadError(f"на странице {page_url} нет ссылки на файл .xlsx - ЦБ мог изменить страницу")
    if len(links) > 1:
        raise LoadError(f"на странице {page_url} несколько файлов .xlsx: {', '.join(links)}")
    url = urllib.parse.urljoin(page_url, links[0])
    if urllib.parse.urlsplit(url).hostname not in ("cbu.uz", "www.cbu.uz"):
        raise LoadError(f"ссылка на файл ведёт не на cbu.uz: {url}")
    return url


def download(fetch=None):
    """Найти файл на странице ЦБ и скачать его. Возвращает (адрес, байты)."""
    fetch = fetch or urllib_fetch  # тесты подменяют urllib_fetch заглушкой
    html = fetch(PAGE_URL).decode("utf-8", errors="replace")
    url = find_file_url(html)
    return url, fetch(url)


# --- Разбор файла -------------------------------------------------------------------

def _text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _date(value, where):
    if value in (None, ""):
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return EXCEL_EPOCH + datetime.timedelta(days=int(value))
    raise LoadError(f"{where}: непонятная дата {value!r}")


def read_rows(content):
    """
    Строки листа «Русский»: [{"mfo": ..., "kind": ..., ..., "raw": {заголовок: значение}}].
    Проверяет формат каждой строки; любая ошибка - LoadError (ничего не загружается).
    """
    try:
        book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, OSError) as error:
        raise LoadError(f"файл ЦБ не читается как Excel: {error}") from error
    if SHEET not in book.sheetnames:
        raise LoadError(f"в файле ЦБ нет листа «{SHEET}» (есть: {', '.join(book.sheetnames)})")

    header = None
    rows = []
    for number, cells in enumerate(book[SHEET].iter_rows(values_only=True), start=1):
        values = [_text(v) for v in cells]
        if header is None:
            if "Код филиала" in values:  # в любой колонке: ЦБ может их переставить
                header = values
                missing = [title for title in REQUIRED if title not in header]
                if missing:
                    raise LoadError(f"в файле ЦБ нет колонок: {', '.join(missing)}")
            continue
        if not any(values):
            continue  # пустая строка
        raw_cells = dict(zip(header, cells))
        raw = {title: _text(value) for title, value in raw_cells.items() if title}
        rows.append(_row(raw, raw_cells, number))
    book.close()

    if header is None:
        raise LoadError("в файле ЦБ не найдена строка заголовков (ячейка «Код филиала»)")
    if not rows:
        raise LoadError("в файле ЦБ нет ни одной строки с банками")
    return rows


def _row(raw, raw_cells, number):
    where = f"файл ЦБ, строка {number}"
    mfo = raw.get("Код филиала", "")
    if mfo.isdigit() and len(mfo) < 5:
        mfo = mfo.zfill(5)  # если Excel хранит код числом, ведущие нули теряются
    if not re.fullmatch(r"[0-9]{5}", mfo):
        raise LoadError(f"{where}: МФО {mfo!r} - не 5 цифр")
    kind = raw.get("Признак филиала", "")
    if not kind.isdigit():
        raise LoadError(f"{where} (МФО {mfo}): признак филиала {kind!r} - не число")
    inn = re.sub(r"\s", "", raw.get("ИНН", ""))
    if inn and not re.fullmatch(r"[0-9]{9}", inn):
        raise LoadError(f"{where} (МФО {mfo}): ИНН {inn!r} - не 9 цифр")
    branch_name = raw.get("Наименование филиала", "")
    if not branch_name:
        raise LoadError(f"{where} (МФО {mfo}): нет наименования")
    return {
        "mfo": mfo,
        "kind": int(kind),
        "bank_name": raw.get("Наименование банка", ""),
        "branch_name": branch_name,
        "inn": inn,
        "address": raw.get("Адрес филиала", ""),
        "region": raw.get("Область", ""),
        "district": raw.get("Район", ""),
        "city": raw.get("Город", ""),
        "opened_on": _date(raw_cells.get("Дата открытия"), f"{where} (МФО {mfo})"),
        "changed_on": _date(raw_cells.get("Дата изменения"), f"{where} (МФО {mfo})"),
        "raw": raw,
    }


# --- Загрузка в базу ----------------------------------------------------------------

def sync(rows, now=None):
    """
    Сверить таблицу CbuBank с разобранным файлом. Вызывается внутри транзакции
    (команда load_cbu_banks). Повтор МФО в файле - LoadError.
    """
    seen = set()
    for row in rows:
        if row["mfo"] in seen:
            raise LoadError(f"файл ЦБ: МФО {row['mfo']} повторяется")
        seen.add(row["mfo"])
    fields = [name for name in COLUMNS.values() if name != "mfo"]
    return sync_reference(
        CbuBank,
        rows,
        lambda row: {name: row[name] for name in fields},
        now,
        id_of=lambda row: row["mfo"],
        raw_of=lambda row: row["raw"],
    )
