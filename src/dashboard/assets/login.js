(function () {
  const A = window.KZ_AUTH;
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const next = /^[a-z-]+\.html$/.test(params.get("next") || "") ? params.get("next") : "dashboard.html";
  const state = { email: "", password: "" };

  function toast(text) {
    const t = $("toast");
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (t.hidden = true), 4000);
  }

  function show(view) {
    document.querySelectorAll("form[data-view]").forEach((f) => (f.hidden = f.dataset.view !== view));
    document.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.tab === view));
    document.querySelectorAll("[data-error]").forEach((e) => (e.hidden = true));
    const first = document.querySelector(`form[data-view="${view}"] input`);
    if (first) first.focus();
  }

  function fail(form, err) {
    const box = form.querySelector("[data-error]");
    box.textContent = err.message || String(err);
    box.hidden = false;
  }

  async function run(form, fn) {
    const btn = form.querySelector("button[type=submit]");
    const text = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Подождите…";
    form.querySelector("[data-error]").hidden = true;
    try {
      await fn();
    } catch (e) {
      fail(form, e);
    } finally {
      btn.disabled = false;
      btn.textContent = text;
    }
  }

  function done() {
    location.href = next;
  }

  document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
  document.querySelectorAll("[data-go]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); show(a.dataset.go); }));

  $("form-login").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    state.email = $("login-email").value.trim().toLowerCase();
    state.password = $("login-password").value;
    run(form, async () => {
      try {
        await A.signIn(state.email, state.password);
        done();
      } catch (err) {
        if (err.code === "UserNotConfirmedException") {
          await A.resend(state.email).catch(() => {});
          $("confirm-email").textContent = state.email;
          show("confirm");
          return;
        }
        throw err;
      }
    });
  });

  $("form-signup").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    state.email = $("signup-email").value.trim().toLowerCase();
    state.password = $("signup-password").value;
    run(form, async () => {
      if (!/^\S+@\S+\.\S+$/.test(state.email)) throw new Error("Проверьте почту.");
      await A.signUp(state.email, state.password);
      $("confirm-email").textContent = state.email;
      show("confirm");
    });
  });

  $("form-confirm").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    run(form, async () => {
      await A.confirm(state.email, $("confirm-code").value.trim());
      if (state.password) {
        await A.signIn(state.email, state.password);
        done();
      } else {
        toast("Почта подтверждена, теперь войдите.");
        $("login-email").value = state.email;
        show("login");
      }
    });
  });

  $("resend-link").addEventListener("click", async (e) => {
    e.preventDefault();
    try {
      await A.resend(state.email);
      toast("Код отправлен ещё раз.");
    } catch (err) {
      toast(err.message);
    }
  });

  $("form-forgot").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    state.email = $("forgot-email").value.trim().toLowerCase();
    run(form, async () => {
      await A.forgot(state.email);
      $("reset-email").textContent = state.email;
      show("reset");
    });
  });

  $("form-reset").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    state.password = $("reset-password").value;
    run(form, async () => {
      await A.reset(state.email, $("reset-code").value.trim(), state.password);
      await A.signIn(state.email, state.password);
      done();
    });
  });

  if (!A.configured()) {
    document.querySelectorAll("form button[type=submit]").forEach((b) => (b.disabled = true));
    toast("Вход ещё не настроен: адреса Cognito появятся после terraform apply.");
  } else if (A.loggedIn()) {
    done();
  } else {
    show(params.get("tab") === "signup" ? "signup" : "login");
  }
})();
