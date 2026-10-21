"""
Формы кадровых действий в карточке пользователя (часть A3, docs/users-model.md):
новый пользователь сразу с местом работы, «Принять», «Перевести», «Уволить»,
смена своего пароля.

Формы только собирают данные; правила - в accounts/personnel.py.
"""
from django import forms
from django.contrib.admin.forms import AdminPasswordChangeForm
from django.contrib.admin.widgets import AdminDateWidget
from django.contrib.auth.forms import UsernameField
from django.utils import timezone

from companies.models import Department

from .apps import BLANK_CHOICE_LABEL
from .models import Assignment, JobPosition, Role, User, Visibility


def _today():
    return timezone.localdate()


class PlaceFields(forms.Form):
    """Место работы: с какой даты, где, кем, с какими ролями."""

    valid_from = forms.DateField(label="С даты", initial=_today, widget=AdminDateWidget)
    # Подпись пустого пункта - явно: эти поля создаются при загрузке админ-панели,
    # раньше, чем accounts/apps.py заменяет английскую подпись Django.
    department = forms.ModelChoiceField(label="Подразделение", queryset=Department.objects.none(),
                                        empty_label=BLANK_CHOICE_LABEL)
    position = forms.ModelChoiceField(label="Должность", queryset=JobPosition.objects.none(),
                                      empty_label=BLANK_CHOICE_LABEL)
    roles = forms.ModelMultipleChoiceField(
        label="Роли",
        queryset=Role.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Что человек может делать. Можно несколько.",
    )
    visibility = forms.ChoiceField(label="Видимость", choices=Visibility.choices, initial=Visibility.COMPANY)
    is_head = forms.BooleanField(label="Руководитель подразделения", required=False)
    basis = forms.CharField(label="Основание", max_length=255, required=False,
                            help_text="Приказ, соглашение ГПХ и т. п.")

    PLACE_FIELDS = ["valid_from", "department", "position", "roles", "visibility", "is_head", "basis"]

    def limit_places(self, company_id):
        """Списки выбора - только своей страховой (company_id пусто - все страховые)."""
        departments = Department.objects.filter(status=Department.Status.ACTIVE).select_related(
            "parent", "insurance_company"
        )
        if company_id is not None:
            self.fields["department"].queryset = departments.filter(insurance_company_id=company_id)
            self.fields["department"].label_from_instance = lambda d: d.full_name()
        else:
            self.fields["department"].queryset = departments
            self.fields["department"].label_from_instance = lambda d: f"{d.insurance_company}: {d.full_name()}"
        self.fields["position"].queryset = JobPosition.objects.filter(is_active=True)
        self.fields["roles"].queryset = Role.objects.filter(audience=Role.Audience.COMPANY).select_related("group")

    def clean(self):
        data = super().clean()
        if data.get("is_head") and data.get("visibility") == Visibility.OWN:
            self.add_error("visibility", "Руководитель видит не меньше своего подразделения.")
        return data

    def place(self):
        data = self.cleaned_data
        return {name: data.get(name) for name in ("department", "position", "visibility", "is_head", "basis")}


class HireForm(PlaceFields):
    valid_to = forms.DateField(
        label="По дату", required=False, widget=AdminDateWidget,
        help_text="Включительно. Пусто - бессрочно (срочный договор, ГПХ - дата окончания).",
    )


class TransferForm(PlaceFields):
    valid_from = forms.DateField(
        label="Дата перевода", initial=_today, widget=AdminDateWidget,
        help_text="Первый день на новом месте. Нынешнее место закроется накануне.",
    )


class DismissForm(forms.Form):
    last_day = forms.DateField(
        label="Последний рабочий день", initial=_today, widget=AdminDateWidget,
        help_text=(
            "Включительно. Сегодня или раньше - учётная запись блокируется сразу; "
            "дата впереди - вход закроется сам на следующий день после неё."
        ),
    )


class UserAddForm(PlaceFields, forms.ModelForm):
    """
    Новый пользователь - одним экраном: учётная запись и сразу место работы.
    Пароль не вводится: временный пароль выдаёт система после сохранения.
    Администратору платформы место не нужно - поля места для него необязательны.
    """

    class Meta:
        model = User
        fields = ["username"]
        field_classes = {"username": UsernameField}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.PLACE_FIELDS:
            if name in self.fields:
                self.fields[name].required = False

    def clean(self):
        data = super().clean()
        kind = data.get("kind", self.instance.kind)
        if kind == User.Kind.PLATFORM:
            return data
        company = data.get("insurance_company") if "insurance_company" in self.fields else None
        company_id = company.pk if company is not None else self.instance.insurance_company_id
        for name in ("valid_from", "department", "position"):
            if not data.get(name) and name not in self.errors:
                self.add_error(name, "Укажите место работы: без него сотрудник не сможет войти.")
        department = data.get("department")
        if department is not None and company_id is not None and department.insurance_company_id != company_id:
            self.add_error("department", "Это подразделение другой страховой.")
        if department is not None and data.get("is_head") and data.get("valid_from"):
            head = Assignment.objects.filter(department=department, is_head=True).exclude(
                valid_to__lt=data["valid_from"]
            ).first()
            if head is not None:
                self.add_error("is_head", f"У подразделения уже есть руководитель: {head.user.full_name()}.")
        return data


class OwnPasswordChangeForm(AdminPasswordChangeForm):
    """
    Смена своего пароля. Отличие от стандартной: в журнал пишется, что пароль
    сменил сам человек (а не «система»), и снимается отметка «сменить пароль
    при входе».
    """

    def save(self, commit=True):
        self.user.set_password(self.cleaned_data["new_password1"])
        self.user.must_change_password = False
        if commit:
            self.user.save(by=self.user)
        return self.user
