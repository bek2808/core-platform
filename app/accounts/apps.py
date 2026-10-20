from django.apps import AppConfig

# Подпись пустого пункта выпадающего списка. В Django 6.1 она новая -
# «- Select an option -» - и русского перевода у неё ещё нет, поэтому во всех
# списках системы стояла английская фраза. Django советует задавать свою
# подпись именно здесь, в ready().
BLANK_CHOICE_LABEL = "—"


class AccountsConfig(AppConfig):
    name = "accounts"
    verbose_name = "Пользователи"

    def ready(self):
        from django.db.models import fields

        fields.BLANK_CHOICE_LABEL = BLANK_CHOICE_LABEL
