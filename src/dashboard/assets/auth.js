// Вход в аккаунт Kezekshi через Amazon Cognito (без сторонних библиотек).
// Токены хранятся в localStorage этого браузера. window.KZ_AUTH используют остальные страницы.
(function () {
  const cfg = window.APP_CONFIG || {};
  const KEY = "kz_session";
  const endpoint = () => `https://cognito-idp.${cfg.COGNITO_REGION}.amazonaws.com/`;

  const ERRORS = {
    UsernameExistsException: "Аккаунт с этой почтой уже есть. Войдите или восстановите пароль.",
    NotAuthorizedException: "Неверная почта или пароль.",
    UserNotConfirmedException: "Почта ещё не подтверждена. Введите код из письма.",
    CodeMismatchException: "Неверный код.",
    ExpiredCodeException: "Код устарел, запросите новый.",
    InvalidPasswordException: "Пароль: минимум 8 символов, строчные буквы и цифры.",
    InvalidParameterException: "Проверьте почту и пароль.",
    LimitExceededException: "Слишком много попыток, подождите немного.",
    TooManyRequestsException: "Слишком много попыток, подождите немного.",
    UserNotFoundException: "Неверная почта или пароль.",
  };

  async function cognito(action, body) {
    if (!cfg.COGNITO_REGION || !cfg.COGNITO_CLIENT_ID) throw new Error("Вход не настроен (COGNITO_* в config.js).");
    const res = await fetch(endpoint(), {
      method: "POST",
      headers: {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": "AWSCognitoIdentityProviderService." + action,
      },
      body: JSON.stringify(Object.assign({ ClientId: cfg.COGNITO_CLIENT_ID }, body)),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const type = String(data.__type || "").split("#").pop();
      const err = new Error(ERRORS[type] || data.message || "Ошибка входа");
      err.code = type;
      throw err;
    }
    return data;
  }

  function decode(jwt) {
    const part = jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(decodeURIComponent(escape(atob(part))));
  }

  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) { return null; }
  }
  function save(s) {
    try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) { /* приватный режим */ }
  }
  function clear() {
    try { localStorage.removeItem(KEY); } catch (e) { /* ничего */ }
  }

  function store(result, email, refreshToken) {
    const claims = decode(result.IdToken);
    const s = {
      idToken: result.IdToken,
      refreshToken: result.RefreshToken || refreshToken,
      email: claims.email || email,
      exp: claims.exp * 1000,
    };
    save(s);
    return s;
  }

  const api = {
    configured: () => Boolean(cfg.COGNITO_REGION && cfg.COGNITO_CLIENT_ID),
    session: load,
    email: () => (load() || {}).email || "",
    loggedIn: () => Boolean(load()),

    signUp: (email, password) => cognito("SignUp", {
      Username: email, Password: password, UserAttributes: [{ Name: "email", Value: email }],
    }),
    confirm: (email, code) => cognito("ConfirmSignUp", { Username: email, ConfirmationCode: code }),
    resend: (email) => cognito("ResendConfirmationCode", { Username: email }),
    forgot: (email) => cognito("ForgotPassword", { Username: email }),
    reset: (email, code, password) => cognito("ConfirmForgotPassword", {
      Username: email, ConfirmationCode: code, Password: password,
    }),

    async signIn(email, password) {
      const data = await cognito("InitiateAuth", {
        AuthFlow: "USER_PASSWORD_AUTH", AuthParameters: { USERNAME: email, PASSWORD: password },
      });
      return store(data.AuthenticationResult, email);
    },

    signOut() {
      clear();
    },

    // Свежий токен: если до конца срока меньше 2 минут, обновляем по refresh token
    async token() {
      const s = load();
      if (!s) return null;
      if (Date.now() < s.exp - 120000) return s.idToken;
      try {
        const data = await cognito("InitiateAuth", {
          AuthFlow: "REFRESH_TOKEN_AUTH", AuthParameters: { REFRESH_TOKEN: s.refreshToken },
        });
        return store(data.AuthenticationResult, s.email, s.refreshToken).idToken;
      } catch (e) {
        clear();
        return null;
      }
    },

    // Запрос к личному кабинету с токеном. 401 = сессия кончилась, отправляем на вход
    async call(path, options) {
      const token = await api.token();
      if (!token) {
        location.href = "login.html?next=" + encodeURIComponent(location.pathname.split("/").pop() || "dashboard.html");
        throw new Error("login required");
      }
      const opts = Object.assign({ method: "GET" }, options || {});
      opts.headers = Object.assign({ Authorization: token }, opts.body ? { "Content-Type": "application/json" } : {}, opts.headers || {});
      const base = path.startsWith("http") ? "" : (cfg.ACCOUNT_API_URL || "").replace(/\/$/, "");
      const res = await fetch(base + path, opts);
      if (res.status === 401) {
        clear();
        location.href = "login.html?next=" + encodeURIComponent(location.pathname.split("/").pop() || "dashboard.html");
        throw new Error("session expired");
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
      return data;
    },
  };
  window.KZ_AUTH = api;

  // В шапке: «Войти» или почта + «Выйти»
  document.addEventListener("DOMContentLoaded", () => {
    const nav = document.querySelector(".nav");
    if (!nav || nav.querySelector(".nav-auth")) return;
    const box = document.createElement("span");
    box.className = "nav-auth";
    if (api.loggedIn()) {
      box.innerHTML = `<span class="nav-user" title="Вы вошли"></span><a href="#" id="logout-link">Выйти</a>`;
      box.querySelector(".nav-user").textContent = api.email();
      box.querySelector("#logout-link").addEventListener("click", (e) => {
        e.preventDefault();
        api.signOut();
        location.href = "index.html";
      });
    } else {
      const here = location.pathname.endsWith("login.html");
      box.innerHTML = `<a href="login.html"${here ? ' aria-current="page"' : ""}>Войти</a>`;
    }
    nav.appendChild(box);
  });
})();
