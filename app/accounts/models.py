"""
Модель пользователя Core-системы.

Стандартная модель Django (логин, ФИО, e-mail, пароль, группы и права)
плюс страховая компания, в которой работает сотрудник. Своя модель заведена
с самого начала намеренно: в неё добавляются поля (филиал, должность)
без болезненной миграции.
"""
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    insurance_company = models.ForeignKey(
        "companies.InsuranceCompany",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="users",
        verbose_name="страховая компания",
        help_text=(
            "Компания, в которой работает сотрудник: он видит только её клиентов. "
            "Пусто - только у администраторов системы."
        ),
    )

    class Meta:
        verbose_name = "пользователь"
        verbose_name_plural = "пользователи"
