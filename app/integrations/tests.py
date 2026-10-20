"""
Автотесты модуля интеграции: клиент ЕРСП (вход, токен в памяти, повтор
на 401, разбор ошибок), журнал обмена (секреты скрыты, только пополняется),
админ-панель журнала, команда ersp_check.

В сеть тесты НЕ ходят: вместо ЕРСП - заглушка FakeErsp, которая отвечает
заранее заданными ответами и запоминает, что ей отправили. Данные для входа
Mosaic - в карточке подключения (ErspMixin), ключ шифрования и токены -
во временной папке вместо /run/secrets.

Запуск на сервере: bin/test integrations
"""
import io
import json
import tempfile
from pathlib import Path

from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command
from django.db import DatabaseError, connection, transaction
from django.test import SimpleTestCase, TestCase, override_settings

from accounts.models import User
from accounts.testing import give_roles
from companies.models import InsuranceCompany
from config.test_crypto import write_key
from config.testing import refusal_expected

from . import ersp
from .ersp import ErspClient, ErspConfigError, ErspError, environment_for, is_error, load_credentials, mask
from .models import Environment, ErspConnection, ErspCredentials, Exchange

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"

CREDENTIALS = {
    "client_id": "5_test_client",
    "client_secret": "client-secret-VALUE",
    "username": "mosaic_ins",
    "password": "pa\"ss\\word-VALUE",
}
TOKEN = "access-token-VALUE"
REFRESH = "refresh-token-VALUE"
SECRET_VALUES = [CREDENTIALS["client_secret"], CREDENTIALS["password"], TOKEN, REFRESH]

# Настоящий ответ провайдера по Inson (из требований Бекзода).
INSON = {
    "name_short": "\"INSON\" AJ ST",
    "nameShort": "\"INSON\" AJ ST",
    "phoneNumber": None,
    "fund": 85751457400,
    "gdFullName": "NIYAZMETOV ILXOMJON IKRAMBAYEVICH",
    "districtSoatoCode": "1726277",
    "bankMfo": "01095",
    "account": "20216000004849419001",
    "okpo": "23047346",
    "name": "\"INSON\" AKSIYADORLIK JAMIYATI SUG'URTA TASHKILOTI",
    "address": "Labzak MFY, Zulfiyaxonim ko'chasi, 12-uy  ",
    "phone": "998998944440",
    "oked": "65120",
    "okedTitle": "Hаyotni sug`urtаlаshdаn tаshqаri sug`urtаlаsh",
}


def token_answer(token=TOKEN, expires_in=3600):
    return 200, {
        "error": "0",
        "error_message": "",
        "result": {
            "access_token": token,
            "expires_in": expires_in,
            "token_type": "bearer",
            "scope": None,
            "refresh_token": REFRESH,
        },
    }


def provider_answer(result=INSON):
    return 200, {"error": 0, "error_message": "", "result": result}


class FakeErsp:
    """
    Заглушка ЕРСП. answers - очередь ответов: (код HTTP, тело) или исключение.
    Тело - dict (уйдёт как JSON) или bytes (как есть).
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body)})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, payload = answer
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return status, raw

    def paths(self):
        return [call["url"].split(".uz", 1)[1] for call in self.calls]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def fill_credentials(company, environments=Environment.values, values=CREDENTIALS, by=None):
    """Заполнить карточку подключения страховой (как администратор на экране)."""
    connection = ErspConnection.for_company(company, by=by)
    for credentials in connection.credentials.filter(environment__in=environments):
        credentials.client_id = values["client_id"]
        credentials.username = values["username"]
        credentials.set_secret("client_secret", values["client_secret"])
        credentials.set_secret("password", values["password"])
        credentials.save(by=by)
    return connection


class ErspMixin:
    """
    Временная папка вместо /run/secrets: ключ шифрования и папка токенов.
    У страховой Mosaic (cls.mosaic) заполнены данные для входа обеих сред.
    Подклассы со своим setUpTestData вызывают super().setUpTestData() первым.
    """

    @classmethod
    def setUpClass(cls):
        cls._secrets_folder = tempfile.TemporaryDirectory()
        cls.secrets = Path(cls._secrets_folder.name)
        write_key(cls.secrets)
        cls.token_dir = cls.secrets / "tokens"
        cls._settings_override = override_settings(SECRETS_DIR=cls.secrets, ERSP_TOKEN_DIR=cls.token_dir)
        cls._settings_override.enable()
        try:
            super().setUpClass()
        except Exception:
            cls._settings_override.disable()
            cls._secrets_folder.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._settings_override.disable()
        cls._secrets_folder.cleanup()

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.mosaic = InsuranceCompany.objects.filter(code="mosaic").first() or InsuranceCompany.objects.create(
            name="Mosaic Insurance Group", code="mosaic"
        )
        fill_credentials(cls.mosaic)

    def setUp(self):
        super().setUp()
        ersp.forget_tokens()
        self.addCleanup(ersp.forget_tokens)


class HelperTests(SimpleTestCase):
    def test_mask_hides_secrets_at_any_depth(self):
        data = {"password": "x", "result": {"access_token": "t", "refresh_token": "r", "expires_in": 5}}
        self.assertEqual(
            mask(data),
            {"password": "***", "result": {"access_token": "***", "refresh_token": "***", "expires_in": 5}},
        )

    def test_mask_keeps_empty_values_visible(self):
        # Пустой токен в ответе - важная подробность при разборе, а не секрет.
        self.assertEqual(mask({"access_token": ""}), {"access_token": ""})

    def test_is_error(self):
        for ok in (0, "0", "", None):
            self.assertFalse(is_error({"error": ok}), ok)
        for bad in (1, "1", 404, "invalid_grant"):
            self.assertTrue(is_error({"error": bad}), bad)


class CredentialsTests(ErspMixin, TestCase):
    """Данные для входа - из карточки подключения своей страховой."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other = InsuranceCompany.objects.create(name="Test Insurance KZ", code="test-kz", country="KZ")

    def test_loaded_from_card(self):
        self.assertEqual(load_credentials(self.mosaic, "prod"), CREDENTIALS)

    def test_other_company_never_gets_mosaic_credentials(self):
        with self.assertRaisesMessage(ErspConfigError, "не заполнено подключение"):
            load_credentials(self.other, "prod")
        own = {**CREDENTIALS, "client_id": "kz_client", "password": "kz-password"}
        fill_credentials(self.other, values=own)
        self.assertEqual(load_credentials(self.other, "prod"), own)
        self.assertEqual(load_credentials(self.mosaic, "prod"), CREDENTIALS)

    def test_incomplete_card_explained(self):
        ErspConnection.for_company(self.other)
        client = ErspClient("prod", self.other, transport=FakeErsp())
        with self.assertRaisesMessage(ErspConfigError, "нет: client_id, client_secret, логин, пароль"):
            client.token_info()
        self.assertFalse(Exchange.objects.exists())  # до ЕРСП дело не дошло

    def test_undecryptable_explained(self):
        write_key(self.secrets)  # ключ заменили - старые шифры не читаются
        self.addCleanup(write_key, self.secrets)
        with self.assertRaisesMessage(ErspConfigError, "Введите его заново"):
            ErspClient("prod", self.mosaic, transport=FakeErsp()).token_info()
        self.assertFalse(Exchange.objects.exists())

    def test_company_required(self):
        with self.assertRaisesMessage(ErspConfigError, "Не указана страховая"):
            ErspClient("prod", None)

    def test_unknown_environment(self):
        with self.assertRaises(ErspConfigError):
            ErspClient("test", self.mosaic)

    def test_environment_by_purpose(self):
        # Справочники и провайдер - всегда боевая; отправка - из карточки.
        self.assertEqual(environment_for(ersp.REFERENCES, self.mosaic), "prod")
        self.assertEqual(environment_for(ersp.PROVIDER, self.mosaic), "prod")
        self.assertEqual(environment_for(ersp.SEND, self.mosaic), "sandbox")
        self.assertEqual(environment_for(ersp.SEND, self.other), "sandbox")  # карточки нет - песочница
        connection = ErspConnection.objects.get(insurance_company=self.mosaic)
        connection.send_environment = "prod"
        connection.save()
        self.assertEqual(environment_for(ersp.SEND, self.mosaic), "prod")
        self.assertEqual(ErspClient.for_purpose(ersp.SEND, self.mosaic).environment, "prod")
        with self.assertRaises(ErspConfigError):
            environment_for("policies", self.mosaic)


class ClientTests(ErspMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.bekzod = User.objects.create_user("bekzod", password=STRONG_PASSWORD, insurance_company=cls.mosaic)

    def client_for(self, fake, environment="prod", clock=None, company=None, **kwargs):
        return ErspClient(environment, company or self.mosaic, transport=fake, clock=clock or Clock(), **kwargs)

    def test_login_request(self):
        fake = FakeErsp(token_answer())
        info = self.client_for(fake).token_info()
        self.assertEqual(info, {"token_type": "bearer", "expires_in": 3600, "scope": None})
        call = fake.calls[0]
        self.assertEqual(call["url"], "https://erspapiv2.e-osgo.uz/oauth/v2/token")
        self.assertEqual(call["body"], {**CREDENTIALS, "grant_type": "password"})
        self.assertNotIn("Authorization", call["headers"])

    def test_environments_have_own_addresses(self):
        fake = FakeErsp(token_answer())
        self.client_for(fake, "sandbox").token_info()
        self.assertTrue(fake.calls[0]["url"].startswith("https://sandboxerspapiv2.e-osgo.uz/"))
        self.assertEqual(Exchange.objects.get().environment, "sandbox")

    def test_provider_inn(self):
        fake = FakeErsp(token_answer(), provider_answer())
        result = self.client_for(fake, company=self.mosaic, user=self.bekzod).provider_inn("207078596")
        self.assertEqual(result["gdFullName"], "NIYAZMETOV ILXOMJON IKRAMBAYEVICH")
        self.assertEqual(fake.paths(), ["/oauth/v2/token", "/api/provider/inn"])
        self.assertEqual(fake.calls[1]["body"], {"inn": "207078596"})
        self.assertEqual(fake.calls[1]["headers"]["Authorization"], f"Bearer {TOKEN}")

        exchange = Exchange.objects.get(operation=Exchange.Operation.PROVIDER_INN)
        self.assertEqual(exchange.outcome, Exchange.Outcome.OK)
        self.assertEqual(exchange.subject, "207078596")
        self.assertEqual(exchange.user, self.bekzod)
        self.assertEqual(exchange.insurance_company, self.mosaic)
        self.assertEqual(exchange.http_status, 200)
        # Ответ хранится целиком, как пришёл (с пробелами в адресе и т. п.).
        self.assertEqual(exchange.response_body["result"], INSON)
        self.assertIsNotNone(exchange.duration_ms)

    def test_system_request_logged_under_company(self):
        fake = FakeErsp(token_answer())
        self.client_for(fake).token_info()
        exchange = Exchange.objects.get()
        self.assertIsNone(exchange.user)
        self.assertEqual(exchange.insurance_company, self.mosaic)

    def test_token_reused_until_it_expires(self):
        clock = Clock()
        fake = FakeErsp(token_answer(expires_in=3600), provider_answer(), provider_answer(),
                        token_answer(token="second-token"), provider_answer())
        client = self.client_for(fake, clock=clock)
        client.provider_inn("207078596")
        clock.now += 3000
        client.provider_inn("207078596")  # токен ещё жив - входа нет
        clock.now += 600  # до конца срока меньше минуты запаса - новый вход
        client.provider_inn("207078596")
        self.assertEqual(
            fake.paths(),
            ["/oauth/v2/token", "/api/provider/inn", "/api/provider/inn", "/oauth/v2/token", "/api/provider/inn"],
        )
        self.assertEqual(fake.calls[-1]["headers"]["Authorization"], "Bearer second-token")

    def test_token_shared_between_clients_of_one_process(self):
        fake = FakeErsp(token_answer(), provider_answer(), provider_answer())
        self.client_for(fake).provider_inn("207078596")
        self.client_for(fake).provider_inn("308143734")
        self.assertEqual(fake.paths().count("/oauth/v2/token"), 1)

    def test_token_without_lifetime_kept_until_401(self):
        clock = Clock()
        fake = FakeErsp(token_answer(expires_in=0), provider_answer(), provider_answer())
        client = self.client_for(fake, clock=clock)
        client.provider_inn("207078596")
        clock.now += 10 * 24 * 3600
        client.provider_inn("207078596")
        self.assertEqual(fake.paths().count("/oauth/v2/token"), 1)

    def test_401_means_new_login_and_one_retry(self):
        fake = FakeErsp(token_answer(), (401, {"error": "invalid_token"}),
                        token_answer(token="fresh-token"), provider_answer())
        result = self.client_for(fake).provider_inn("207078596")
        self.assertEqual(result["okpo"], "23047346")
        self.assertEqual(fake.paths(), ["/oauth/v2/token", "/api/provider/inn", "/oauth/v2/token", "/api/provider/inn"])
        self.assertEqual(fake.calls[-1]["headers"]["Authorization"], "Bearer fresh-token")
        outcomes = list(Exchange.objects.order_by("id").values_list("operation", "outcome"))
        self.assertEqual(outcomes, [
            ("token", "ok"), ("provider_inn", "http_error"), ("token", "ok"), ("provider_inn", "ok"),
        ])

    def test_second_401_stops(self):
        fake = FakeErsp(token_answer(), (401, {}), token_answer(), (401, {}))
        with self.assertRaisesMessage(ErspError, "Подключение к ЕРСП"):
            self.client_for(fake).provider_inn("207078596")
        self.assertEqual(len(fake.calls), 4)  # повтор только один

    def test_wrong_credentials(self):
        fake = FakeErsp((400, {"error": "invalid_grant", "error_message": "Invalid username and password"}))
        with self.assertRaisesMessage(ErspError, "Invalid username and password") as caught:
            self.client_for(fake).token_info()
        self.assertEqual(caught.exception.exchange.outcome, Exchange.Outcome.HTTP_ERROR)

    def test_ersp_error_in_answer(self):
        fake = FakeErsp(token_answer(), (200, {"error": 404, "error_message": "Организация не найдена", "result": None}))
        with self.assertRaisesMessage(ErspError, "Организация не найдена") as caught:
            self.client_for(fake).provider_inn("000000000")
        exchange = caught.exception.exchange
        self.assertEqual(exchange.outcome, Exchange.Outcome.ERSP_ERROR)
        self.assertEqual(exchange.error_code, "404")
        self.assertEqual(exchange.subject, "000000000")

    def test_server_error_page(self):
        fake = FakeErsp(token_answer(), (502, b"<html>Bad Gateway</html>"))
        with self.assertRaisesMessage(ErspError, "HTTP 502"):
            self.client_for(fake).provider_inn("207078596")
        exchange = Exchange.objects.get(operation=Exchange.Operation.PROVIDER_INN)
        self.assertEqual(exchange.outcome, Exchange.Outcome.HTTP_ERROR)
        self.assertEqual(exchange.response_text, "<html>Bad Gateway</html>")
        self.assertIsNone(exchange.response_body)

    def test_no_connection(self):
        fake = FakeErsp(TimeoutError("timed out"))
        with self.assertRaisesMessage(ErspError, "Нет связи с ЕРСП (prod)"):
            self.client_for(fake).token_info()
        exchange = Exchange.objects.get()
        self.assertEqual(exchange.outcome, Exchange.Outcome.NETWORK_ERROR)
        self.assertIsNone(exchange.http_status)

    def test_answer_not_json(self):
        fake = FakeErsp(token_answer(), (200, b"OK"))
        with self.assertRaisesMessage(ErspError, "не в формате JSON"):
            self.client_for(fake).provider_inn("207078596")

    def test_answer_without_result(self):
        fake = FakeErsp(token_answer(), (200, {"error": 0, "error_message": ""}))
        with self.assertRaisesMessage(ErspError, "нет данных"):
            self.client_for(fake).provider_inn("207078596")

    def test_token_answer_without_token(self):
        fake = FakeErsp((200, {"error": "0", "result": {"expires_in": 3600}}))
        with self.assertRaisesMessage(ErspError, "нет токена"):
            self.client_for(fake).token_info()
        self.assertEqual(Exchange.objects.get().outcome, Exchange.Outcome.BAD_RESPONSE)

    def test_standard_oauth_answer_understood(self):
        # Если ЕРСП ответит по стандарту OAuth (поля сверху, без "result").
        fake = FakeErsp((200, {"access_token": TOKEN, "expires_in": 3600, "token_type": "bearer"}), provider_answer())
        self.client_for(fake).provider_inn("207078596")
        self.assertEqual(fake.calls[1]["headers"]["Authorization"], f"Bearer {TOKEN}")

    def test_no_secrets_in_journal(self):
        fake = FakeErsp(token_answer(), (401, {}), token_answer(), provider_answer())
        self.client_for(fake).provider_inn("207078596")
        everything = json.dumps(
            list(Exchange.objects.values()), ensure_ascii=False, default=str
        )
        for value in SECRET_VALUES:
            self.assertNotIn(value, everything)
        login = Exchange.objects.filter(operation=Exchange.Operation.TOKEN).first()
        self.assertEqual(login.request_body["password"], "***")
        self.assertEqual(login.request_body["client_secret"], "***")
        self.assertEqual(login.request_body["username"], "mosaic_ins")  # логин не секрет
        self.assertEqual(login.response_body["result"]["access_token"], "***")
        self.assertEqual(login.response_body["result"]["expires_in"], 3600)


class SharedTokenTests(ErspMixin, TestCase):
    """Один токен на всё приложение (на страховую и среду): файл в папке токенов (в контейнере - tmpfs)."""

    def test_file_readable_only_by_application(self):
        ErspClient("prod", self.mosaic, transport=FakeErsp(token_answer())).token_info()
        self.assertEqual(self.token_dir.stat().st_mode & 0o777, 0o700)
        token_file = self.token_dir / "mosaic.prod.json"
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
        content = token_file.read_text()
        self.assertIn(TOKEN, content)
        for value in (CREDENTIALS["password"], CREDENTIALS["client_secret"]):
            self.assertNotIn(value, content)  # в файле только токен и срок

    def test_another_process_uses_same_token(self):
        # Каждый ErspClient читает общий файл - как соседний процесс gunicorn.
        fake = FakeErsp(token_answer(), provider_answer(), provider_answer())
        ErspClient("prod", self.mosaic, transport=fake).provider_inn("207078596")
        ersp._memory_store.clear()  # памяти процесса тут нет - только файл
        ErspClient("prod", self.mosaic, transport=fake).provider_inn("308143734")
        self.assertEqual(fake.paths().count("/oauth/v2/token"), 1)

    def test_environments_have_separate_tokens(self):
        fake = FakeErsp(token_answer(token="prod-token"), provider_answer(),
                        token_answer(token="sandbox-token"), provider_answer())
        ErspClient("prod", self.mosaic, transport=fake).provider_inn("207078596")
        ErspClient("sandbox", self.mosaic, transport=fake).provider_inn("207078596")
        self.assertEqual(fake.calls[1]["headers"]["Authorization"], "Bearer prod-token")
        self.assertEqual(fake.calls[3]["headers"]["Authorization"], "Bearer sandbox-token")

    def test_login_holds_lock(self):
        # Пока один процесс входит, другой ждёт: блокировка файлом занята.
        import fcntl
        import os

        held = []

        def transport(method, url, headers, body, timeout):
            fd = os.open(self.token_dir / "mosaic.prod.lock", os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)
            return FakeErsp(token_answer())(method, url, headers, body, timeout)

        ErspClient("prod", self.mosaic, transport=transport).token_info()
        self.assertEqual(held, [True])

    def test_rejected_token_does_not_erase_fresh_one(self):
        store = ersp.token_store()
        store.write("mosaic.prod", "fresh-from-neighbour", None)
        store.drop("mosaic.prod", "old-rejected")
        self.assertEqual(store.read("mosaic.prod")[0], "fresh-from-neighbour")
        store.drop("mosaic.prod", "fresh-from-neighbour")
        self.assertIsNone(store.read("mosaic.prod"))

    def test_expiry_shared_by_wall_clock(self):
        clock = Clock()
        fake = FakeErsp(token_answer(expires_in=3600), provider_answer(),
                        token_answer(token="second-token"), provider_answer())
        ErspClient("prod", self.mosaic, transport=fake, clock=clock).provider_inn("207078596")
        clock.now += 3600
        ErspClient("prod", self.mosaic, transport=fake, clock=clock).provider_inn("207078596")
        self.assertEqual(fake.paths().count("/oauth/v2/token"), 2)

    def test_companies_have_separate_tokens(self):
        other = InsuranceCompany.objects.create(name="Test Insurance KZ", code="test-kz", country="KZ")
        fill_credentials(other, values={**CREDENTIALS, "username": "kz_ins"})
        fake = FakeErsp(token_answer(token="mosaic-token"), provider_answer(),
                        token_answer(token="kz-token"), provider_answer())
        ErspClient("prod", self.mosaic, transport=fake).provider_inn("207078596")
        ErspClient("prod", other, transport=fake).provider_inn("207078596")
        self.assertEqual(fake.calls[2]["body"]["username"], "kz_ins")  # вход своими данными
        self.assertEqual(fake.calls[3]["headers"]["Authorization"], "Bearer kz-token")

    def test_status_without_token(self):
        clock = Clock()
        self.assertIsNone(ersp.token_status(self.mosaic, "prod"))
        ErspClient("prod", self.mosaic, transport=FakeErsp(token_answer(expires_in=3600)), clock=clock).token_info()
        status = ersp.token_status(self.mosaic, "prod")
        self.assertEqual(status, {"obtained_at": 1000.0, "expires_at": 4600.0})
        self.assertIsNone(ersp.token_status(self.mosaic, "sandbox"))

    def test_status_when_lifetime_unknown(self):
        ErspClient("prod", self.mosaic, transport=FakeErsp(token_answer(expires_in=0)), clock=Clock()).token_info()
        self.assertEqual(ersp.token_status(self.mosaic, "prod"), {"obtained_at": 1000.0, "expires_at": None})

    def test_forget_token_means_new_login(self):
        fake = FakeErsp(token_answer(), provider_answer(), token_answer(token="after-change"), provider_answer())
        ErspClient("prod", self.mosaic, transport=fake).provider_inn("207078596")
        ersp.forget_token(self.mosaic, "prod")
        self.assertIsNone(ersp.token_status(self.mosaic, "prod"))
        ErspClient("prod", self.mosaic, transport=fake).provider_inn("207078596")
        self.assertEqual(fake.paths().count("/oauth/v2/token"), 2)
        self.assertEqual(fake.calls[-1]["headers"]["Authorization"], "Bearer after-change")

    def test_unusable_folder_falls_back_to_memory(self):
        blocker = self.secrets / "not-a-folder"
        blocker.write_text("x")
        with override_settings(ERSP_TOKEN_DIR=blocker / "tokens"), self.assertLogs("integrations.ersp", "WARNING"):
            fake = FakeErsp(token_answer(), provider_answer(), provider_answer())
            ErspClient("prod", self.mosaic, transport=fake).provider_inn("207078596")
            ErspClient("prod", self.mosaic, transport=fake).provider_inn("207078596")
        self.assertEqual(fake.paths().count("/oauth/v2/token"), 1)


class AppendOnlyTests(ErspMixin, TestCase):
    """Журнал обмена только пополняется: базу защищает триггер (integrations/0001)."""

    def setUp(self):
        super().setUp()
        ErspClient("prod", self.mosaic, transport=FakeErsp(token_answer())).token_info()
        self.exchange = Exchange.objects.get()

    def test_entry_cannot_be_changed_or_deleted(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            Exchange.objects.filter(pk=self.exchange.pk).update(outcome="ok", error_message="подделка")
        with self.assertRaises(DatabaseError), transaction.atomic():
            Exchange.objects.filter(pk=self.exchange.pk).delete()

    def test_raw_sql_also_blocked(self):
        for sql in ["UPDATE integrations_exchange SET subject = 'x'", "DELETE FROM integrations_exchange",
                    "TRUNCATE integrations_exchange"]:
            with self.subTest(sql=sql), self.assertRaises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(sql)
        self.assertTrue(Exchange.objects.filter(pk=self.exchange.pk).exists())


class ExchangeAdminTests(ErspMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other = InsuranceCompany.objects.create(name="Test Insurance KZ", code="test-kz", country="KZ")
        fill_credentials(cls.other)

    def setUp(self):
        super().setUp()
        ErspClient("prod", company=self.mosaic, transport=FakeErsp(token_answer(), provider_answer())).provider_inn("207078596")
        ersp.forget_tokens()
        ErspClient("prod", company=self.other, transport=FakeErsp(token_answer(), provider_answer({"name": "ЧУЖОЙ"}))).provider_inn("999999999")
        self.own = Exchange.objects.get(subject="207078596")
        self.foreign = Exchange.objects.get(subject="999999999")

    def login(self, group):
        user = User.objects.create_user(
            "ivanov", password=STRONG_PASSWORD, is_staff=True, insurance_company=self.mosaic
        )
        give_roles(user, group)
        self.client.force_login(user)
        return user

    def test_groups_may_view(self):
        for name in ("Администратор справочников", "Сотрудник", "Администратор интеграций"):
            self.assertIn(
                "view_exchange",
                Group.objects.get(name=name).permissions.values_list("codename", flat=True),
            )

    def test_employee_sees_only_own_company(self):
        self.login("Сотрудник")
        response = self.client.get("/admin/integrations/exchange/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "207078596")
        self.assertNotContains(response, "999999999")
        self.assertEqual(self.client.get(f"/admin/integrations/exchange/{self.foreign.pk}/change/").status_code, 302)

    def test_card_shows_full_answer(self):
        self.login("Администратор справочников")
        response = self.client.get(f"/admin/integrations/exchange/{self.own.pk}/change/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "NIYAZMETOV ILXOMJON IKRAMBAYEVICH")

    def test_read_only_even_for_superuser(self):
        admin = User.objects.create_superuser("bekzod", password=STRONG_PASSWORD)
        self.client.force_login(admin)
        self.assertContains(self.client.get("/admin/integrations/exchange/"), "999999999")  # видит всё
        with refusal_expected(self):
            self.assertEqual(self.client.get("/admin/integrations/exchange/add/").status_code, 403)
        with refusal_expected(self):
            self.client.post(f"/admin/integrations/exchange/{self.own.pk}/change/", {"subject": "x"})
        with refusal_expected(self):
            self.client.post(f"/admin/integrations/exchange/{self.own.pk}/delete/", {"post": "yes"})
        self.own.refresh_from_db()
        self.assertEqual(self.own.subject, "207078596")


class CheckCommandTests(ErspMixin, TestCase):
    """Команда ersp_check: что видит человек на экране сервера."""

    def run_check(self, fake, *args):
        original = ersp.urllib_transport
        ersp.urllib_transport = fake  # вместо настоящей сети - заглушка
        self.addCleanup(setattr, ersp, "urllib_transport", original)
        out = io.StringIO()
        call_command("ersp_check", *args, stdout=out)
        return out.getvalue()

    def test_both_environments_by_default(self):
        output = self.run_check(FakeErsp(token_answer(expires_in=86400), token_answer(expires_in=3600)))
        self.assertIn("mosaic prod: вход выполнен;", output)
        self.assertIn("действует 86400 с (1 дн.)", output)
        self.assertIn("mosaic sandbox: вход выполнен", output)
        self.assertIn("3600 с (1 ч)", output)
        for value in SECRET_VALUES:
            self.assertNotIn(value, output)

    def test_with_inn(self):
        output = self.run_check(FakeErsp(token_answer(), provider_answer()), "--env", "prod", "--inn", "207078596")
        self.assertIn("провайдер ответил - \"INSON\" AKSIYADORLIK JAMIYATI", output)

    def test_company_chosen_by_code(self):
        output = self.run_check(FakeErsp(token_answer()), "--company", "mosaic", "--env", "prod")
        self.assertIn("mosaic prod: вход выполнен", output)
        with self.assertRaisesMessage(CommandError, "'nope'"):
            self.run_check(FakeErsp(), "--company", "nope")

    def test_company_without_card_reported(self):
        # Карточки нет - понятное сообщение на экране, а не трассировка.
        InsuranceCompany.objects.create(name="Test Insurance KZ", code="test-kz", country="KZ")
        out = io.StringIO()
        with self.assertRaisesMessage(CommandError, "журнале обмена"):
            call_command("ersp_check", "--company", "test-kz", "--env", "prod", stdout=out)
        self.assertIn("test-kz prod: вход не выполнен - У страховой «Test Insurance KZ» не заполнено", out.getvalue())

    def test_failure_reported(self):
        fake = FakeErsp((400, {"error": "invalid_grant", "error_message": "Invalid username and password"}))
        with self.assertRaisesMessage(CommandError, "журнале обмена"):
            self.run_check(fake, "--env", "prod")
