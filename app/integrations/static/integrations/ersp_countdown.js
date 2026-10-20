// Счётчик «сколько осталось» у токена ЕРСП на карточке подключения.
// Сервер ставит у элемента data-ersp-countdown - время окончания срока
// (секунды Unix); здесь оставшееся время пересчитывается раз в секунду.
// Сам токен на страницу не попадает - только его срок.
(function () {
  "use strict";

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  function text(seconds) {
    if (seconds <= 0) { return "срок истёк"; }
    var days = Math.floor(seconds / 86400);
    var rest = seconds % 86400;
    var clock = pad(Math.floor(rest / 3600)) + ":" + pad(Math.floor(rest % 3600 / 60)) + ":" + pad(rest % 60);
    return (days ? days + " дн. " : "") + clock;
  }

  function tick() {
    var now = Math.floor(Date.now() / 1000);
    document.querySelectorAll("[data-ersp-countdown]").forEach(function (el) {
      var until = parseInt(el.getAttribute("data-ersp-countdown"), 10);
      if (!isNaN(until)) { el.textContent = text(until - now); }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    tick();
    setInterval(tick, 1000);
  });
})();
