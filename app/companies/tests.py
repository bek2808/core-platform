"""
Автотесты страховых компаний: обязательные поля для Узбекистана, одна компания
на запись ЕРСП, защита от удаления, привязка сотрудников.

Запуск на сервере: bin/test companies
"""
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from accounts.models import User
from config.testing import refusal_expected
from references.loader import load_from_files
from references.models import ErspInsurer, ErspRegion

from .models import InsuranceCompany

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"


class InsuranceCompanyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        load_from_files()  # один раз на группу тестов

    def setUp(self):
        self.mosaic_ersp = ErspInsurer.objects.get(pk=1084)
        self.tashkent = ErspRegion.objects.get(pk=10)

    def make_mosaic(self, **extra):
        fields = {
            "name": "Mosaic Insurance Group",
            "code": "mosaic",
            "ersp_insurer": self.mosaic_ersp,
            "ersp_region": self.tashkent,
            **extra,
        }
        return InsuranceCompany(**fields)

    def test_valid_uzbek_company(self):
        company = self.make_mosaic()
        company.full_clean()
        company.save()
        self.assertEqual(company.country, "UZ")
        self.assertEqual(self.mosaic_ersp.company, company)

    def test_uzbek_company_needs_ersp_record_and_region(self):
        company = self.make_mosaic(ersp_insurer=None, ersp_region=None)
        with self.assertRaises(ValidationError) as error:
            company.full_clean()
        self.assertEqual(set(error.exception.message_dict), {"ersp_insurer", "ersp_region"})

    def test_foreign_company_without_ersp_allowed(self):
        company = InsuranceCompany(name="Test Insurance KZ", code="test-kz", country="KZ")
        company.full_clean()  # не падает

    def test_country_code_format(self):
        with self.assertRaises(ValidationError):
            self.make_mosaic(country="uz").full_clean()

    def test_one_company_per_ersp_record(self):
        self.make_mosaic().save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_mosaic(code="mosaic-2").save()

    def test_code_is_unique(self):
        self.make_mosaic().save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            InsuranceCompany.objects.create(name="Другая", code="mosaic", country="KZ")

    def test_used_region_cannot_be_deleted(self):
        self.make_mosaic().save()
        with self.assertRaises(ProtectedError):
            self.tashkent.delete()

    def test_employee_linked_to_company(self):
        company = self.make_mosaic()
        company.save()
        user = User.objects.create_user("ivanov", password=STRONG_PASSWORD, insurance_company=company)
        self.assertEqual(list(company.users.all()), [user])
        # Компанию с сотрудниками удалить нельзя даже из кода.
        with self.assertRaises(ProtectedError):
            company.delete()


class InsuranceCompanyAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        load_from_files()  # один раз на группу тестов
        cls.admin = User.objects.create_superuser("admin", password=STRONG_PASSWORD)

    def setUp(self):
        self.client.force_login(self.admin)

    def test_create_company_in_admin(self):
        page = self.client.post(
            "/admin/companies/insurancecompany/add/",
            {
                "name": "Mosaic Insurance Group",
                "code": "mosaic",
                "country": "UZ",
                "ersp_insurer": "1084",
                "ersp_region": "10",
                "is_active": "on",
            },
        )
        self.assertEqual(page.status_code, 302, page.content.decode()[:2000])
        company = InsuranceCompany.objects.get(code="mosaic")
        self.assertEqual(company.ersp_insurer_id, 1084)

    def test_no_delete_in_admin(self):
        company = InsuranceCompany.objects.create(name="Test", code="test", country="KZ")
        with refusal_expected(self):
            page = self.client.get(f"/admin/companies/insurancecompany/{company.pk}/delete/")
        self.assertEqual(page.status_code, 403)

    def test_user_page_shows_company_field(self):
        user = User.objects.create_user("ivanov", password=STRONG_PASSWORD)
        page = self.client.get(f"/admin/accounts/user/{user.pk}/change/")
        self.assertContains(page, "insurance_company")
