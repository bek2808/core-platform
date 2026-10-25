// Блок «Представители» в карточке контрагента: вместо флажка «Удалить» - кнопка.
// Решение Бекзода 25.10: нажать кнопку, увидеть, что строка помечена, и быть
// уверенным, что она удалится при нажатии «Сохранить».
//
// Удаляет по-прежнему не скрипт, а сервер: кнопка лишь ставит и снимает
// штатный флажок Django (name="representatives-N-DELETE"), он уходит в форме
// вместе с остальными полями. Флажок остаётся в странице, просто скрыт. Если
// скрипт не загрузился, виден обычный флажок. Страница, вернувшаяся с ошибкой
// формы, приходит с уже отмеченным флажком - строка рисуется помеченной сразу.
(function () {
  "use strict";

  var LABEL_DELETE = "Удалить";
  var LABEL_UNDO = "Отменить удаление";
  var NOTICE = "Строка будет удалена после нажатия «Сохранить». Запись об удалении попадёт в «Историю».";

  function setup(checkbox) {
    var row = checkbox.closest(".inline-related");
    var holder = checkbox.closest(".delete");
    var title = row && row.querySelector("h3");
    if (!row || !holder || !title) { return; }

    var button = document.createElement("button");
    button.type = "button";
    button.className = "rep-delete-button";

    var notice = document.createElement("div");
    notice.className = "rep-delete-notice";
    notice.setAttribute("role", "status");
    notice.textContent = NOTICE;

    function show() {
      var marked = checkbox.checked;
      row.classList.toggle("rep-marked", marked);
      button.textContent = marked ? LABEL_UNDO : LABEL_DELETE;
      button.classList.toggle("rep-undo", marked);
      button.setAttribute("aria-pressed", marked ? "true" : "false");
      notice.hidden = !marked;
    }

    button.addEventListener("click", function () {
      checkbox.checked = !checkbox.checked;
      show();
    });

    title.appendChild(button);
    title.insertAdjacentElement("afterend", notice);
    row.classList.add("rep-js");   // с этого момента флажок скрыт стилем
    show();
  }

  function init() {
    document.querySelectorAll('input[type="checkbox"][name^="representatives-"][name$="-DELETE"]')
      .forEach(setup);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
