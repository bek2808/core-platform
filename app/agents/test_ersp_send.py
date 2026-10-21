"""
Автотесты отправки агента в ЕРСП (часть B5c; agents/ersp_send.py, экран «Отправить в ЕРСП»).

Главное, что проверяется:
- пакет запроса: организация и физлицо, все поля по описанию метода, скан-файл,
  «Агент:» / «Broker:» в названии, «ХХХ» вместо пустого отчества;
- «изменить» уходит ПОЛНЫМ набором (те же поля, что при добавлении, плюс id): метод
  update очищает непереданные необязательные поля;
- «чего не хватает»: нет скана, соглашения, места, связи подразделения с ЕРСП,
  счёта, представителя, данных физлица, электронный агент - отправка не начинается;
- форма с файлом доходит так, как её разберёт сервер (настоящий разборщик Django),
  кириллица в имени файла цела, в журнал обмена файл не попадает, секреты скрыты;
- проверка перед отправкой: справочник песочницы обновляется (чтение), дубль ИНН / ПИНФЛ
  и пропавший родитель мешают отправке, в ЕРСП ничего не записывается;
- отправка: только в песочницу (даже если у страховой выбрана боевая), только то, что
  проверено (отпечаток), agencyId из ответа или из справочника, связь записывается;
- экран: отправляет только «Администратор страховой».

В сеть тесты не ходят: ЕРСП заменён заглушкой FakeErsp (integrations/tests.py).

Запуск на сервере: bin/test agents.test_ersp_send
"""
import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from accounts.models import User
from accounts.testing import give_roles
from config.testing import refusal_expected
from counterparties.models import Bank, BankAccount, Counterparty, Individual, Position, Representative
from files.tests import PDF_BYTES, upload
from integrations import ersp
from integrations.models import Environment, ErspAgency, ErspConnection, Exchange
from integrations.tests import ErspMixin, FakeErsp, token_answer
from references.models import ErspCountry, ErspResidentType

from . import ersp_send, services
from .models import Agent, ErspLink
from .test_agents import AGENTS_URL, D, STRONG_PASSWORD, AgentCase, individual

HEAD_ID = 53
INN = "201070533"


def agency_row(ersp_id, name, **fields):
    row = {"id": ersp_id, "name": name, "agencyType": 2, "parentId": HEAD_ID, "inn": "", **fields}
    return row


HEAD_ROW = {"id": HEAD_ID, "name": "direct dep", "agencyType": 1, "inn": "207078596"}


def directory(*rows):
    """Ответ ЕРСП на справочник агентов: головной офис и переданные записи."""
    return 200, {"error": 0, "error_message": "", "result": [HEAD_ROW, *rows]}


def accepted(result=None):
    return 200, {"error": 0, "error_message": "", "result": result}


class SendCase(ErspMixin, AgentCase):
    @classmethod
    def setUpClass(cls):
        cls._files = tempfile.TemporaryDirectory()
        cls._files_settings = override_settings(FILES_ROOT=Path(cls._files.name))
        cls._files_settings.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._files_settings.disable()
        cls._files.cleanup()

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Песочница: головной офис Mosaic = запись 53, связанная с подразделением.
        cls.head_row = ErspAgency.objects.create(
            insurance_company=cls.mosaic, environment=Environment.SANDBOX, ersp_id=HEAD_ID, name="direct dep",
            agency_type=1, inn="207078596", raw=HEAD_ROW, synced_at=timezone.now(),
        )
        ErspLink(ersp_agency=cls.head_row, department=cls.head).save()

    def setUp(self):
        super().setUp()
        self.fake = FakeErsp()
        original = ersp.urllib_transport
        ersp.urllib_transport = self.fake
        self.addCleanup(setattr, ersp, "urllib_transport", original)

    # --- заготовки ---------------------------------------------------------------------

    def complete_organization(self, **agent_fields):
        """Агент-организация, у которой есть всё для отправки."""
        card = self.inre
        card.region_id, card.address, card.phone = 10, "г. Ташкент, ул. Тестовая, 1", "998901234567"
        card.save()
        legal = card.legal_entity
        legal.oked_id = "65120"
        legal.ownership_form_id = None
        legal.save()
        Representative(counterparty=card, full_name="Ivanov Ivan Ivanovich", is_primary=True,
                       position=Position.objects.filter(is_other=False).first()).save()
        account = BankAccount(counterparty=card, bank=Bank.objects.create(mfo="01095", name="AAB"),
                              number="20216000004849419001", is_primary=True)
        account.save()
        agent = self.agent(card, commission_account=account, **agent_fields)
        self.finish(agent)
        return agent

    def finish(self, agent, scan=True, place=True):
        """Место агента, соглашение и скан."""
        if place:
            services.place(agent, department=self.head, valid_from=D(2021, 4, 1), by=self.director)
        agreement = self.agreement(agent)
        if scan:
            services.attach_scan(agreement, upload(PDF_BYTES, "Соглашение №5.pdf"), by=self.director)
        return agreement

    def complete_person(self, **agent_fields):
        """Агент-физлицо, у которого есть всё для отправки."""
        card = self.person_with_data
        card.region_id, card.address, card.phone = 10, "г. Ташкент, ул. Тестовая, 2", "998901112233"
        card.save()
        person = card.individual
        person.resident_type_id = ErspResidentType.CITIZEN
        person.country_id = ErspCountry.UZBEKISTAN
        person.passport_series, person.passport_number = "AD", "0000000"
        person.save()
        agent_fields.setdefault("formation_date", D(2026, 1, 15))
        agent = self.agent(card, **agent_fields)
        self.finish(agent)
        return agent

    @staticmethod
    def fields(request):
        return dict(request.fields)

    def answers(self, *answers):
        """Очередь ответов; токен нужен один раз - дальше он лежит в хранилище."""
        if not getattr(self, "_token_queued", False):
            self._token_queued = True
            self.fake.answers.append(token_answer())
        self.fake.answers += list(answers)


class BuildTests(SendCase):
    def test_organization_package(self):
        agent = self.complete_organization()
        request = ersp_send.build(agent)
        self.assertEqual(request.gaps, [])
        self.assertEqual((request.operation, request.path), ("add", "/api/agencies/add"))
        fields = self.fields(request)
        self.assertEqual(fields["name"], 'Агент: ООО "InRe"')
        self.assertEqual((fields["parentId"], fields["agencyType"], fields["areaTypeId"]), ("53", "2", "1"))
        self.assertEqual((fields["agentAgreementNumber"], fields["agentAgreementDate"]), ("AZM/21-000", "2021-04-14"))
        self.assertEqual((fields["formationDate"], fields["isElectronAgency"]), ("2015-03-01", "0"))
        self.assertNotIn("isHeadElectronAgency", fields)  # у обычного агента поле запрещено (422)
        base = "agency[organization]"
        self.assertEqual(fields[f"{base}[inn]"], INN)
        self.assertEqual(fields[f"{base}[name]"], 'ООО "InRe"')
        self.assertEqual(fields[f"{base}[representativeName]"], "Ivanov Ivan Ivanovich")
        self.assertEqual(fields[f"{base}[checkingAccount]"], "20216000004849419001")
        self.assertEqual((fields[f"{base}[phone]"], fields[f"{base}[regionId]"], fields[f"{base}[oked]"]),
                         ("998901234567", "10", "65120"))
        self.assertFalse([key for key in fields if key.startswith("agency[person]")])
        self.assertNotIn(f"{base}[ownershipFormId]", fields)  # нет формы собственности - не отправляем
        name, content = request.file[1], request.file[2]
        self.assertEqual((name, content), ("Соглашение №5.pdf", PDF_BYTES))
        self.assertEqual(request.file[0], "agencyAgreementFile")

    def test_broker_prefix(self):
        agent = self.complete_organization(kind="broker")
        self.assertEqual(self.fields(ersp_send.build(agent))["name"], 'Broker: ООО "InRe"')

    def test_person_package(self):
        agent = self.complete_person()
        request = ersp_send.build(agent)
        self.assertEqual(request.gaps, [])
        fields = self.fields(request)
        base = "agency[person]"
        self.assertEqual(fields["name"], "Агент: FAMILIYA ISM")
        self.assertEqual(fields["formationDate"], "2026-01-15")
        self.assertEqual((fields[f"{base}[regionId]"], fields[f"{base}[birthDate]"]), ("10", "1990-01-01"))
        self.assertEqual((fields[f"{base}[residentType]"], fields[f"{base}[countryId]"]), ("1", "210"))
        self.assertEqual(fields[f"{base}[phone]"], "998901112233")
        self.assertEqual(fields[f"{base}[passportData][pinfl]"], "30101990000000")
        self.assertEqual((fields[f"{base}[passportData][seria]"], fields[f"{base}[passportData][number]"]),
                         ("AD", "0000000"))
        self.assertEqual((fields[f"{base}[fullName][firstname]"], fields[f"{base}[fullName][lastname]"]),
                         ("ISM", "FAMILIYA"))
        self.assertEqual(fields[f"{base}[fullName][middlename]"], "ХХХ")  # нет отчества
        self.assertFalse([key for key in fields if key.startswith("agency[organization]")])

    def test_person_middle_name_sent_when_present(self):
        agent = self.complete_person()
        person = agent.counterparty.individual
        person.middle_name = "OTCHESTVO"
        person.save()
        self.assertEqual(self.fields(ersp_send.build(agent))["agency[person][fullName][middlename]"], "OTCHESTVO")

    def test_update_sends_full_package_with_id(self):
        """Update очищает непереданное: пакет - тот же полный набор, плюс id."""
        agent = self.complete_organization()
        added = ersp_send.build(agent)
        row = ErspAgency.objects.create(
            insurance_company=self.mosaic, environment=Environment.SANDBOX, ersp_id=777, name="x", agency_type=2,
            inn=INN, raw={}, synced_at=timezone.now(),
        )
        ErspLink(ersp_agency=row, agent=agent).save()
        update = ersp_send.build(agent)
        self.assertEqual((update.operation, update.ersp_id, update.path), ("update", 777, "/api/agencies/update"))
        self.assertEqual([key for key, _ in update.fields if key != "id"], [key for key, _ in added.fields])
        self.assertEqual(update.fields[0], ("id", "777"))
        self.assertEqual(update.file[2], PDF_BYTES)  # файл не теряется
        self.assertEqual(update.gaps, [])

    def test_link_to_deleted_record_blocks(self):
        agent = self.complete_organization()
        row = ErspAgency.objects.create(
            insurance_company=self.mosaic, environment=Environment.SANDBOX, ersp_id=778, name="x", agency_type=2,
            raw={}, synced_at=timezone.now(),
        )
        ErspLink(ersp_agency=row, agent=agent).save()
        row.is_deleted = True
        row.save()
        self.assertTrue(any("удалена в ЕРСП" in gap for gap in ersp_send.build(agent).gaps))

    def test_fingerprint(self):
        agent = self.complete_organization()
        first = ersp_send.build(agent).fingerprint()
        self.assertEqual(first, ersp_send.build(agent).fingerprint())
        card = agent.counterparty
        card.address = "другой адрес"
        card.save()
        self.assertNotEqual(first, ersp_send.build(agent).fingerprint())

    def test_fingerprint_changes_with_scan(self):
        agent = self.complete_organization()
        first = ersp_send.build(agent).fingerprint()
        services.attach_scan(agent.current_agreement(), upload(PDF_BYTES + b"\n%new", "Новый.pdf"), by=self.director)
        self.assertNotEqual(first, ersp_send.build(agent).fingerprint())

    def test_secret_fields_hidden_on_screen(self):
        request = ersp_send.AgencyRequest(agent=None, fields=[
            ("name", "x"), ("electronAgencyApiToken", "SECRET"), ("agency[x][electronAgencyApiToken]", "SECRET2"),
        ])
        shown = dict(request.shown_fields())
        self.assertEqual(shown["name"], "x")
        self.assertEqual(shown["electronAgencyApiToken"], "••••")


class GapTests(SendCase):
    def gaps(self, agent):
        return " | ".join(ersp_send.build(agent).gaps)

    def test_no_scan(self):
        agent = self.agent(self.inre)
        self.finish(agent, scan=False)
        self.assertIn("нет скана", self.gaps(agent))
        self.assertIsNone(ersp_send.build(agent).file)

    def test_scan_file_missing_on_disk(self):
        agent = self.complete_organization()
        scan = agent.current_agreement().scan
        (Path(self._files.name) / scan.path).unlink()
        self.assertIn("файла нет на диске", self.gaps(agent))

    def test_scan_too_big_for_ersp(self):
        agent = self.complete_organization()
        services.attach_scan(agent.current_agreement(), upload(PDF_BYTES + b" " * 1_000_001, "Большой.pdf"),
                             by=self.director)
        self.assertIn("слишком большой", self.gaps(agent))

    def test_no_agreement(self):
        agent = self.agent(self.inre)
        services.place(agent, department=self.head, valid_from=D(2021, 4, 1), by=self.director)
        self.assertIn("Нет действующего соглашения", self.gaps(agent))

    def test_no_placement(self):
        agent = self.agent(self.inre)
        self.finish(agent, place=False)
        self.assertIn("не прикреплён к подразделению", self.gaps(agent))

    def test_department_without_ersp_link(self):
        agent = self.agent(self.inre)
        services.place(agent, department=self.branch, valid_from=D(2021, 4, 1), by=self.director)
        self.agreement(agent)
        self.assertIn("нет живой связи с ЕРСП в песочнице", self.gaps(agent))

    def test_division_uses_head_office_link(self):
        agent = self.complete_organization()
        self.assertEqual(ersp_send.build(agent).parent_ersp_id, HEAD_ID)
        # отдел головного офиса берёт связь головного офиса
        other = self.agent(self.moi)
        services.place(other, department=self.division, valid_from=D(2021, 4, 1), by=self.director)
        self.assertEqual(ersp_send.build(other).parent_ersp_id, HEAD_ID)

    def test_organization_gaps(self):
        agent = self.agent(self.moi)
        self.finish(agent)
        gaps = self.gaps(agent)
        for text in ("нет региона", "нет адреса", "нет ОКЭД", "нет телефона", "нет счёта для комиссии",
                     "нет основного представителя"):
            self.assertIn(text, gaps)

    def test_foreign_organization_refused(self):
        agent = self.complete_organization()
        legal = agent.counterparty.legal_entity
        legal.is_foreign, legal.country = True, "KZ"
        legal.inn = "KZ1234567890"
        legal.save()
        self.assertIn("иностранная", self.gaps(agent))

    def test_electronic_agent_not_sent(self):
        agent = self.complete_organization(is_electronic=True)
        self.assertIn("Электронные агенты отсюда пока не отправляются", self.gaps(agent))

    def test_person_gaps(self):
        agent = self.agent(self.person_with_data, formation_date=D(2026, 1, 1))
        self.finish(agent)
        gaps = self.gaps(agent)
        for text in ("нет региона", "нет адреса", "нет телефона", "не указан тип резидентства",
                     "не указана страна", "нет серии паспорта", "нет номера паспорта"):
            self.assertIn(text, gaps)

    def test_person_citizen_needs_pinfl_but_foreigner_does_not(self):
        agent = self.complete_person()
        person = agent.counterparty.individual
        person.pinfl = ""
        person.save()
        self.assertIn("нет ПИНФЛ", self.gaps(agent))
        person.resident_type_id = ErspResidentType.FOREIGNER
        person.save()
        self.assertNotIn("ПИНФЛ", self.gaps(agent))

    def test_bad_phone_format(self):
        agent = self.complete_person()
        card = agent.counterparty
        card.phone = "1234567"
        card.save()
        self.assertIn("нужен формат 998 и 9 цифр", self.gaps(agent))


class ClientTests(SendCase):
    def test_multipart_reaches_server_intact(self):
        agent = self.complete_organization()
        request = ersp_send.build(agent)
        self.answers(accepted({"id": 900}))
        client = ersp.ErspClient("sandbox", self.mosaic, user=self.director)
        self.assertEqual(client.agencies_add(request.fields, request.file), {"id": 900})
        call = self.fake.calls[1]
        self.assertEqual(call["url"], "https://sandboxerspapiv2.e-osgo.uz/api/agencies/add")
        self.assertTrue(call["headers"]["Content-Type"].startswith("multipart/form-data; boundary="))
        self.assertEqual(call["body"]["fields"], request.fields)  # порядок и значения как собрали
        self.assertEqual(call["body"]["file"], ("agencyAgreementFile", "Соглашение №5.pdf", PDF_BYTES, "application/pdf"))

    def test_journal_has_file_meta_not_bytes_and_hides_secrets(self):
        request = ersp_send.AgencyRequest(agent=None, fields=[("name", "x"), ("electronAgencyApiToken", "SECRET")])
        request.file = ("agencyAgreementFile", "a.pdf", PDF_BYTES, "application/pdf")
        self.answers(accepted())
        client = ersp.ErspClient("sandbox", self.mosaic, user=self.director)
        client.agencies_add(request.fields, request.file)
        row = Exchange.objects.get(operation=Exchange.Operation.AGENCY_ADD)
        self.assertEqual(row.outcome, Exchange.Outcome.OK)
        self.assertEqual(row.request_body["electronAgencyApiToken"], "***")
        self.assertIn("файл a.pdf", row.request_body["agencyAgreementFile"])
        self.assertIn("sha256", row.request_body["agencyAgreementFile"])
        self.assertNotIn("%PDF", str(row.request_body))
        self.assertEqual(row.user, self.director)
        self.assertEqual(row.subject, "x")

    def test_answer_without_result_is_success(self):
        """Запись уже сделана: ответ без result не должен выглядеть как ошибка."""
        self.answers((200, {"error": 0, "error_message": ""}))
        client = ersp.ErspClient("sandbox", self.mosaic, user=self.director)
        self.assertIsNone(client.agencies_add([("name", "x")], None))
        self.assertEqual(Exchange.objects.get(operation=Exchange.Operation.AGENCY_ADD).outcome, "ok")

    def test_update_path(self):
        self.answers(accepted())
        ersp.ErspClient("sandbox", self.mosaic, user=self.director).agencies_update([("id", "5")], None)
        self.assertTrue(self.fake.calls[1]["url"].endswith("/api/agencies/update"))
        self.assertIsNone(self.fake.calls[1]["body"]["file"])

    def test_ersp_error_logged(self):
        self.answers((200, {"error": 400, "error_message": "ПИНФЛ в чёрном списке"}))
        client = ersp.ErspClient("sandbox", self.mosaic, user=self.director)
        with self.assertRaisesMessage(ersp.ErspError, "ПИНФЛ в чёрном списке"):
            client.agencies_add([("name", "x")], None)
        self.assertEqual(Exchange.objects.get(operation=Exchange.Operation.AGENCY_ADD).outcome, "ersp_error")


class CheckTests(SendCase):
    def test_clean_check_is_read_only(self):
        agent = self.complete_organization()
        self.answers(directory())
        result = ersp_send.check(agent, self.director)
        self.assertTrue(result.can_send)
        self.assertEqual(self.fake.paths(), ["/oauth/v2/token", "/api/references/agencies"])  # ничего не записано
        self.assertTrue(any("Родитель: 53" in note for note in result.notes))
        self.assertTrue(any("дубля не будет" in note for note in result.notes))

    def test_duplicate_inn_blocks(self):
        agent = self.complete_organization()
        self.answers(directory(agency_row(701, 'Broker: ООО "InRe"', inn=INN)))
        result = ersp_send.check(agent, self.director)
        self.assertFalse(result.can_send)
        self.assertIn("уже есть агент с таким ИНН: 701", " ".join(result.problems))

    def test_duplicate_pinfl_blocks_person(self):
        agent = self.complete_person()
        row = agency_row(702, "Агент: X", pinfl="30101990000000")
        row["person"] = {"passportData": {"pinfl": "30101990000000"}}
        self.answers(directory(row))
        result = ersp_send.check(agent, self.director)
        self.assertFalse(result.can_send)
        self.assertIn("ПИНФЛ: 702", " ".join(result.problems))

    def test_deleted_duplicate_does_not_block(self):
        agent = self.complete_organization()
        self.answers(directory(agency_row(703, "старый", inn=INN, deletionMark=True)))
        self.assertTrue(ersp_send.check(agent, self.director).can_send)

    def test_missing_parent_blocks(self):
        agent = self.complete_organization()
        # справочник песочницы больше не содержит головной офис 53
        self.answers((200, {"error": 0, "result": [agency_row(1, "другой")]}))
        result = ersp_send.check(agent, self.director)
        self.assertFalse(result.can_send)
        self.assertIn("Родитель 53 не найден", " ".join(result.problems))

    def test_gaps_block_even_if_directory_fine(self):
        agent = self.agent(self.inre)
        self.finish(agent, scan=False)
        self.answers(directory())
        self.assertFalse(ersp_send.check(agent, self.director).can_send)

    def test_own_record_is_not_a_duplicate_on_update(self):
        agent = self.complete_organization()
        row = ErspAgency.objects.create(
            insurance_company=self.mosaic, environment=Environment.SANDBOX, ersp_id=777, name="x", agency_type=2,
            inn=INN, raw={}, synced_at=timezone.now(),
        )
        ErspLink(ersp_agency=row, agent=agent).save()
        self.answers(directory(agency_row(777, "x", inn=INN)))
        result = ersp_send.check(agent, self.director)
        self.assertTrue(result.can_send)
        self.assertEqual(result.request.operation, "update")

    def test_ersp_error_on_directory(self):
        agent = self.complete_organization()
        self.answers((500, {"error_message": "сбой"}))
        with self.assertRaises(ersp.ErspError):
            ersp_send.check(agent, self.director)


class SendTests(SendCase):
    def prepared(self, agent):
        """Проверка пройдена - отпечаток запроса, который видел сотрудник."""
        self.answers(directory())
        check = ersp_send.check(agent, self.director)
        self.assertTrue(check.can_send)
        self.fake.calls.clear()
        return check.request.fingerprint()

    def post_calls(self):
        return [c for c in self.fake.calls if c["method"] == "POST" and "/api/agencies/" in c["url"]]

    def test_add_with_id_in_answer(self):
        agent = self.complete_organization()
        fingerprint = self.prepared(agent)
        self.answers(accepted({"id": 900}), directory(agency_row(900, 'Агент: ООО "InRe"', inn=INN)))
        outcome = ersp_send.send(agent, self.director, fingerprint)
        self.assertTrue(outcome.linked)
        self.assertEqual(outcome.ersp_id, 900)
        link = ErspLink.objects.get(agent=agent)
        self.assertEqual((link.ersp_agency.ersp_id, link.environment, link.created_by), (900, "sandbox", self.director))
        self.assertEqual(len(self.post_calls()), 1)
        self.assertEqual(self.fake.paths(), ["/api/agencies/add", "/api/references/agencies"])

    def test_add_with_id_found_in_directory(self):
        agent = self.complete_organization()
        fingerprint = self.prepared(agent)
        self.answers(accepted(), directory(agency_row(901, 'Агент: ООО "InRe"', inn=INN)))
        outcome = ersp_send.send(agent, self.director, fingerprint)
        self.assertEqual((outcome.linked, outcome.ersp_id), (True, 901))

    def test_person_found_by_pinfl(self):
        agent = self.complete_person()
        fingerprint = self.prepared(agent)
        row = agency_row(902, "Агент: FAMILIYA ISM", pinfl="30101990000000")
        row["person"] = {"passportData": {"pinfl": "30101990000000"}}
        self.answers(accepted({"something": "else"}), directory(row))
        outcome = ersp_send.send(agent, self.director, fingerprint)
        self.assertEqual((outcome.linked, outcome.ersp_id), (True, 902))

    def test_id_not_found_gives_message_and_no_link(self):
        agent = self.complete_organization()
        fingerprint = self.prepared(agent)
        self.answers(accepted(), directory())
        outcome = ersp_send.send(agent, self.director, fingerprint)
        self.assertFalse(outcome.linked)
        self.assertIn("agencyId определить не удалось", outcome.message)
        self.assertFalse(ErspLink.objects.filter(agent=agent).exists())

    def test_directory_refresh_failure_after_send(self):
        agent = self.complete_organization()
        fingerprint = self.prepared(agent)
        self.answers(accepted({"id": 900}), (500, {"error_message": "сбой"}))
        outcome = ersp_send.send(agent, self.director, fingerprint)
        self.assertFalse(outcome.linked)
        self.assertIn("справочник обновить не удалось", outcome.message)

    def test_update_for_linked_agent(self):
        agent = self.complete_organization()
        row = ErspAgency.objects.create(
            insurance_company=self.mosaic, environment=Environment.SANDBOX, ersp_id=777, name="x", agency_type=2,
            inn=INN, raw={}, synced_at=timezone.now(),
        )
        ErspLink(ersp_agency=row, agent=agent).save()
        self.answers(directory(agency_row(777, "x", inn=INN)))
        fingerprint = ersp_send.check(agent, self.director).request.fingerprint()
        self.fake.calls.clear()
        self.answers(accepted(), directory(agency_row(777, "x", inn=INN)))
        outcome = ersp_send.send(agent, self.director, fingerprint)
        self.assertEqual((outcome.operation, outcome.ersp_id, outcome.linked), ("update", 777, False))
        (call,) = self.post_calls()
        self.assertTrue(call["url"].endswith("/api/agencies/update"))
        self.assertEqual(call["body"]["fields"][0], ("id", "777"))
        self.assertEqual(ErspLink.objects.filter(agent=agent).count(), 1)

    def test_changed_data_refused_without_calls(self):
        agent = self.complete_organization()
        fingerprint = self.prepared(agent)
        card = agent.counterparty
        card.address = "поменяли после проверки"
        card.save()
        with self.assertRaisesMessage(ersp_send.SendRefused, "изменились с момента проверки"):
            ersp_send.send(agent, self.director, fingerprint)
        self.assertEqual(self.fake.calls, [])

    def test_wrong_fingerprint_refused(self):
        agent = self.complete_organization()
        with self.assertRaises(ersp_send.SendRefused):
            ersp_send.send(agent, self.director, "не тот отпечаток")
        self.assertEqual(self.fake.calls, [])

    def test_gaps_refused_without_calls(self):
        agent = self.agent(self.inre)
        self.finish(agent, scan=False)
        with self.assertRaisesMessage(ersp_send.SendRefused, "нет скана"):
            ersp_send.send(agent, self.director, ersp_send.build(agent).fingerprint())
        self.assertEqual(self.fake.calls, [])

    def test_duplicate_in_local_directory_refused(self):
        agent = self.complete_organization()
        fingerprint = ersp_send.build(agent).fingerprint()
        ErspAgency.objects.create(
            insurance_company=self.mosaic, environment=Environment.SANDBOX, ersp_id=950, name="кто-то", agency_type=2,
            inn=INN, raw={}, synced_at=timezone.now(),
        )
        with self.assertRaisesMessage(ersp_send.SendRefused, "уже есть агент с таким ИНН"):
            ersp_send.send(agent, self.director, fingerprint)
        self.assertEqual(self.fake.calls, [])

    def test_always_sandbox_even_if_company_sends_to_prod(self):
        connection = ErspConnection.for_company(self.mosaic)
        connection.send_environment = Environment.PROD
        connection.save()
        agent = self.complete_organization()
        fingerprint = self.prepared(agent)
        self.answers(accepted({"id": 900}), directory(agency_row(900, "x", inn=INN)))
        ersp_send.send(agent, self.director, fingerprint)
        self.assertTrue(all(c["url"].startswith("https://sandboxerspapiv2") for c in self.fake.calls))
        self.assertEqual(ErspLink.objects.get(agent=agent).environment, "sandbox")

    def test_ersp_rejection_leaves_no_link(self):
        agent = self.complete_organization()
        fingerprint = self.prepared(agent)
        self.answers((200, {"error": 400, "error_message": "Агент с таким ИНН уже есть"}))
        with self.assertRaisesMessage(ersp.ErspError, "Агент с таким ИНН уже есть"):
            ersp_send.send(agent, self.director, fingerprint)
        self.assertFalse(ErspLink.objects.filter(agent=agent).exists())
        self.assertEqual(Exchange.objects.get(operation=Exchange.Operation.AGENCY_ADD).outcome, "ersp_error")


class ScreenTests(SendCase):
    def login(self, *groups, username="admin_mosaic"):
        user = User.objects.create_user(username, password=STRONG_PASSWORD, insurance_company=self.mosaic)
        give_roles(user, *groups)
        self.client.force_login(user)
        return user

    def url(self, agent):
        return f"{AGENTS_URL}{agent.pk}/ersp/"

    def test_button_and_page_for_company_admin(self):
        agent = self.complete_organization()
        self.login("Администратор страховой")
        card = self.client.get(f"{AGENTS_URL}{agent.pk}/change/")
        self.assertContains(card, "Отправить в ЕРСП")
        page = self.client.get(self.url(agent))
        self.assertContains(page, "песочница ЕРСП")
        self.assertContains(page, "agency[organization][inn]")
        self.assertContains(page, "Соглашение №5.pdf")
        self.assertContains(page, "Проверить перед отправкой")
        self.assertNotContains(page, "Отправить в песочницу ЕРСП")  # до проверки кнопки отправки нет
        self.assertEqual(self.fake.calls, [])  # страница сама в ЕРСП не ходит

    def test_agents_role_cannot_send(self):
        agent = self.complete_organization()
        self.login("Работа с агентами", username="sales")
        self.assertNotContains(self.client.get(f"{AGENTS_URL}{agent.pk}/change/"), "Отправить в ЕРСП")
        with refusal_expected(self):
            self.assertEqual(self.client.get(self.url(agent)).status_code, 403)
            self.assertEqual(self.client.post(self.url(agent), {"action": "check"}).status_code, 403)
        self.assertEqual(self.fake.calls, [])

    def test_employee_and_platform_cannot_send(self):
        agent = self.complete_organization()
        self.login("Сотрудник", username="plain")
        with refusal_expected(self):
            self.assertEqual(self.client.get(self.url(agent)).status_code, 403)
        platform = User.objects.create_user("platform", password=STRONG_PASSWORD, is_staff=True)
        give_roles(platform, "Администратор страховой")
        self.client.force_login(platform)
        with refusal_expected(self):  # у платформы нет страховой: чужих агентов она не видит
            self.assertIn(self.client.get(self.url(agent)).status_code, (403, 404))

    def test_other_company_agent_not_found(self):
        other = Agent(counterparty=self.foreign, area_type_id=1)
        other.save()
        self.login("Администратор страховой")
        with refusal_expected(self):
            self.assertEqual(self.client.get(self.url(other)).status_code, 404)

    def test_gaps_shown_and_no_send_button(self):
        agent = self.agent(self.inre)
        self.finish(agent, scan=False)
        self.login("Администратор страховой")
        self.answers(directory())
        page = self.client.post(self.url(agent), {"action": "check"})
        self.assertContains(page, "Отправка невозможна")
        self.assertContains(page, "нет скана")
        self.assertNotContains(page, "Отправить в песочницу ЕРСП")

    def test_check_then_send_flow(self):
        agent = self.complete_organization()
        self.login("Администратор страховой")
        self.answers(directory())
        page = self.client.post(self.url(agent), {"action": "check"})
        self.assertContains(page, "Отправить в песочницу ЕРСП")
        self.assertContains(page, "Справочник ЕРСП (песочница) обновлён")
        fingerprint = page.context["fingerprint"]
        self.assertEqual(self.fake.paths(), ["/oauth/v2/token", "/api/references/agencies"])

        # без отметки «проверил» - отказ, в ЕРСП ничего не уходит
        before = len(self.fake.calls)
        page = self.client.post(self.url(agent), {"action": "send", "fingerprint": fingerprint})
        self.assertContains(page, "Отметьте, что вы проверили запрос")
        self.assertEqual(len(self.fake.calls), before)

        self.fake.answers += [accepted({"id": 900}), directory(agency_row(900, "x", inn=INN))]
        response = self.client.post(self.url(agent), {"action": "send", "fingerprint": fingerprint, "confirm": "1"})
        self.assertRedirects(response, f"{AGENTS_URL}{agent.pk}/change/", fetch_redirect_response=False)
        self.assertEqual(ErspLink.objects.get(agent=agent).ersp_agency.ersp_id, 900)
        follow = self.client.get(f"{AGENTS_URL}{agent.pk}/change/")
        self.assertContains(follow, "agencyId 900")

    def test_stale_fingerprint_shows_message(self):
        agent = self.complete_organization()
        self.login("Администратор страховой")
        page = self.client.post(self.url(agent), {"action": "send", "fingerprint": "x", "confirm": "1"})
        self.assertContains(page, "изменились с момента проверки")
        self.assertEqual(self.fake.calls, [])

    def test_ersp_rejection_shown(self):
        agent = self.complete_organization()
        self.login("Администратор страховой")
        self.answers(directory())
        fingerprint = self.client.post(self.url(agent), {"action": "check"}).context["fingerprint"]
        self.fake.answers += [(200, {"error": 400, "error_message": "Скан не подходит"})]
        page = self.client.post(self.url(agent), {"action": "send", "fingerprint": fingerprint, "confirm": "1"})
        self.assertContains(page, "ЕРСП: ЕРСП: Скан не подходит")
        self.assertFalse(ErspLink.objects.filter(agent=agent).exists())

    def test_directory_failure_shown(self):
        agent = self.complete_organization()
        self.login("Администратор страховой")
        self.answers((500, {"error_message": "сбой"}))
        page = self.client.post(self.url(agent), {"action": "check"})
        self.assertContains(page, "Справочник ЕРСП не загружен")
        self.assertNotContains(page, "Отправить в песочницу ЕРСП")
