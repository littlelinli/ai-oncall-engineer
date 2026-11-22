(function () {
  const form = document.getElementById("connect-form");
  const input = document.getElementById("server-name");
  const error = document.getElementById("server-error");
  const result = document.getElementById("result");
  const cmd = document.getElementById("install-cmd");
  const toast = document.getElementById("toast");
  const submitBtn = form.querySelector('button[type="submit"]');
  const submitText = submitBtn.textContent;

  const cfg = window.APP_CONFIG || {};
  const REGISTER_URL = (cfg.REGISTER_URL || "").trim();

  function showToast(text) {
    toast.textContent = text;
    toast.hidden = false;
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => (toast.hidden = true), 4000);
  }

  function showError(text) {
    error.textContent = text;
    error.hidden = false;
    input.setAttribute("aria-invalid", "true");
    input.focus();
  }

  input.addEventListener("input", () => {
    error.hidden = true;
    input.removeAttribute("aria-invalid");
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = input.value.trim();
    // те же правила, что в /register и install.sh
    if (!/^[A-Za-z0-9._-]{1,63}$/.test(name)) {
      showError("Введите имя: латинские буквы, цифры, точка, _ и -, до 63 символов, без пробелов.");
      return;
    }
    if (!REGISTER_URL) {
      showError("Адрес /register не задан в assets/config.js (REGISTER_URL).");
      return;
    }

    const cloudInput = form.querySelector('input[name="cloud"]:checked');
    submitBtn.disabled = true;
    submitBtn.textContent = "Получаем команду…";
    try {
      const resp = await fetch(REGISTER_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, cloud: cloudInput ? cloudInput.value : "aws" }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.command) {
        showError(data.error || "Не удалось получить команду (код " + resp.status + "). Попробуйте ещё раз.");
        return;
      }
      document.getElementById("result-name").textContent = data.name || name;
      cmd.textContent = data.command;
      result.hidden = false;
      result.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (err) {
      // сюда попадаем при ошибке сети или CORS
      showError("Сервер регистрации недоступен. Проверьте интернет; если ошибка повторяется, нужно включить CORS в API Gateway.");
      console.error("register failed", err);
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = submitText;
    }
  });

  document.getElementById("copy-btn").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(cmd.textContent);
      showToast("Команда скопирована");
    } catch (e) {
      showToast("Не удалось скопировать, выделите команду вручную");
    }
  });
})();
