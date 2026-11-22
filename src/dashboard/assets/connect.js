(function () {
  const form = document.getElementById("connect-form");
  const input = document.getElementById("server-name");
  const error = document.getElementById("server-error");
  const result = document.getElementById("result");
  const cmd = document.getElementById("install-cmd");
  const toast = document.getElementById("toast");

  function showToast(text) {
    toast.textContent = text;
    toast.hidden = false;
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => (toast.hidden = true), 3000);
  }

  function demoKey() {
    const chars = "abcdefghijkmnpqrstuvwxyz23456789";
    let k = "kz_demo_";
    for (let i = 0; i < 24; i++) k += chars[Math.floor(Math.random() * chars.length)];
    return k;
  }

  input.addEventListener("input", () => {
    error.hidden = true;
    input.removeAttribute("aria-invalid");
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const name = input.value.trim();
    if (!/^[a-zA-Z0-9-]{3,40}$/.test(name)) {
      error.hidden = false;
      input.setAttribute("aria-invalid", "true");
      input.focus();
      return;
    }
    document.getElementById("result-name").textContent = name;
    cmd.textContent =
      "curl -fsSL https://get.kezekshi.kz/agent.sh | sudo bash -s -- \\\n" +
      "  --server " + name + " \\\n" +
      "  --cloud aws \\\n" +
      "  --key " + demoKey();
    result.hidden = false;
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
