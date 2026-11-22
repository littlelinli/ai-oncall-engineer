(function () {
  const cfg = window.APP_CONFIG || {};
  const state = { incidents: [], filter: "all", selectedId: null };

  const SEVERITY = { critical: "Критичный", high: "Высокий", medium: "Средний", low: "Низкий" };
  const STATUS = { open: "Открыт", resolved: "Решён" };

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function fmtTime(iso) {
    const d = new Date(iso);
    return d.toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }
  function fmtDuration(ms) {
    const min = Math.round(ms / 60000);
    if (min < 60) return min + " мин";
    const h = Math.floor(min / 60);
    return h + " ч " + (min % 60) + " мин";
  }

  function toast(text) {
    const t = $("toast");
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (t.hidden = true), 3500);
  }

  // Приводит инцидент к одному виду: понимает и формат из docs/incident-format.md,
  // и формат Lambda №3 (timestamp, recommendation_steps, logs_sample, log_summary, metric/value/threshold).
  function prettyLog(line) {
    try {
      const o = JSON.parse(line);
      const t = o.time ? new Date(o.time).toLocaleTimeString("ru-RU", { hour12: false }) : "";
      const extra = Object.entries(o)
        .filter(([k, v]) => !["time", "level", "msg"].includes(k) && v != null)
        .map(([k, v]) => `${k}=${v}`).join(" ");
      return `${t} ${String(o.level || "").padEnd(5)} ${o.msg || ""}${extra ? "  " + extra : ""}`.trim();
    } catch (e) {
      return String(line);
    }
  }

  function normalize(raw) {
    const i = Object.assign({}, raw);
    i.incident_id = i.incident_id || i.id;
    i.started_at = i.started_at || i.timestamp;
    i.recommendations = i.recommendations || i.recommendation_steps || [];
    i.logs = i.logs || (i.logs_sample || []).map(prettyLog);
    i.log_summary = Array.isArray(i.log_summary) ? i.log_summary : [];
    if (!i.summary) {
      const top = [...i.log_summary].sort((a, b) => (b.count || 0) - (a.count || 0))[0];
      i.summary = top
        ? `За период перед тревогой ${top.count} раз записано «${top.msg}»${top.path ? " на " + top.path : ""}${top.status ? " (ответ " + top.status + ")" : ""}.`
        : i.reason || "";
    }
    const st = i.stats || {};
    i.stats = {
      cpu_percent: st.cpu_percent ?? st.cpu,
      disk_used_percent: st.disk_used_percent ?? st.disk,
      error_rate_percent: st.error_rate_percent,
      p95_latency_ms: st.p95_latency_ms
    };
    if (!i.auto_fix) {
      i.auto_fix = {
        applied: false,
        action: null,
        result: i.auto_fix_possible
          ? "AI считает, что проблему можно исправить автоматически. Автопочинка в этой версии выключена."
          : "Не применялась"
      };
    }
    if (!i.service) {
      const ns = i.metric ? String(i.metric).split("/")[0] : "";
      i.service = ns && !/^(CWAgent|AWS)/.test(ns) ? ns : "demo-app";
    }
    if (i.instance_id === "unknown") i.instance_id = null;
    return i;
  }

  async function loadIncidents() {
    if (!cfg.API_URL) {
      state.incidents = (window.MOCK_INCIDENTS || []).map(normalize);
      return;
    }
    try {
      const res = await fetch(cfg.API_URL.replace(/\/$/, "") + "/incidents");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      state.incidents = (Array.isArray(data) ? data : data.items || []).map(normalize);
      const withRegion = state.incidents.find((x) => x.region);
      if (withRegion) $("page-sub").textContent = `Сервис ${withRegion.service || "demo-app"}, регион ${withRegion.region}`;
      const pill = $("source-pill");
      pill.textContent = "Данные из AWS";
      pill.classList.add("is-live");
    } catch (e) {
      state.incidents = (window.MOCK_INCIDENTS || []).map(normalize);
      toast("Не удалось загрузить данные из API, показаны тестовые. Проверьте API_URL и CORS.");
    }
  }

  function sorted() {
    return [...state.incidents].sort((a, b) => new Date(b.started_at) - new Date(a.started_at));
  }

  function renderStats() {
    const list = state.incidents;
    const weekAgo = Date.now() - 7 * 864e5;
    $("stat-open").textContent = list.filter((i) => i.status === "open").length;
    $("stat-week").textContent = list.filter((i) => new Date(i.started_at) >= weekAgo).length;
    const done = list.filter((i) => i.resolved_at);
    if (done.length) {
      const avg = done.reduce((s, i) => s + (new Date(i.resolved_at) - new Date(i.started_at)), 0) / done.length;
      $("stat-mttr").textContent = fmtDuration(avg);
    }
  }

  function renderChart() {
    const svg = $("chart");
    const W = 600, H = 180, pad = 24, days = 14;
    const buckets = [];
    const today = new Date(); today.setHours(0, 0, 0, 0);
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(today); d.setDate(d.getDate() - i);
      buckets.push({ date: d, hi: 0, lo: 0 });
    }
    state.incidents.forEach((inc) => {
      const d = new Date(inc.started_at); d.setHours(0, 0, 0, 0);
      const b = buckets.find((x) => x.date.getTime() === d.getTime());
      if (!b) return;
      if (inc.severity === "critical" || inc.severity === "high") b.hi++; else b.lo++;
    });
    const max = Math.max(2, ...buckets.map((b) => b.hi + b.lo));
    const bw = (W - pad) / days;
    const plotH = H - pad;
    let out = "";
    for (let g = 0; g <= max; g++) {
      const y = plotH - (g / max) * (plotH - 8);
      out += `<line x1="${pad}" x2="${W}" y1="${y}" y2="${y}" stroke="var(--line)" stroke-width="1"/>`;
      out += `<text x="${pad - 8}" y="${y + 4}" fill="var(--muted)" font-size="11" text-anchor="end">${g}</text>`;
    }
    buckets.forEach((b, i) => {
      const x = pad + i * bw + bw * 0.22, w = bw * 0.56;
      const unit = (plotH - 8) / max;
      let y = plotH;
      if (b.lo) { const h = b.lo * unit; y -= h; out += `<rect x="${x}" y="${y}" width="${w}" height="${h - 1}" rx="2" fill="#7f9bd1"/>`; }
      if (b.hi) { const h = b.hi * unit; y -= h; out += `<rect x="${x}" y="${y}" width="${w}" height="${h - 1}" rx="2" fill="var(--alert)"/>`; }
      if (i % 2 === (days - 1) % 2) {
        out += `<text x="${x + w / 2}" y="${H - 4}" fill="var(--muted)" font-size="11" text-anchor="middle">${b.date.getDate()}.${String(b.date.getMonth() + 1).padStart(2, "0")}</text>`;
      }
    });
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.innerHTML = out;
  }

  const ALARM_LABEL = { cpu: "Высокая загрузка CPU", disk: "Диск заполняется", error: "Ошибки приложения", health: "Сервис не отвечает", status: "Проверка статуса EC2" };
  function alarmLabel(name) {
    const n = String(name || "").toLowerCase();
    const key = Object.keys(ALARM_LABEL).find((k) => n.includes(k));
    return key ? ALARM_LABEL[key] : name;
  }

  function plural(n, one, few, many) {
    const m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }

  function renderAlarms() {
    const ul = $("alarm-list");
    const groups = {};
    sorted().forEach((i) => {
      const g = groups[i.alarm_name] || (groups[i.alarm_name] = { name: i.alarm_name, count: 0, last: i, open: false });
      g.count++;
      if (i.status === "open") g.open = true;
    });
    const list = Object.values(groups).sort((a, b) => b.count - a.count);
    if (!list.length) {
      ul.innerHTML = `<li class="detail-empty">Тревог пока не было.</li>`;
      return;
    }
    const max = Math.max(...list.map((g) => g.count));
    ul.innerHTML = list.map((g) => `
      <li class="alarm-item">
        <button type="button" data-id="${esc(g.last.incident_id)}">
          <span class="alarm-top">
            <span class="alarm-name">${esc(alarmLabel(g.name))}</span>
            <span class="status ${g.open ? "status-open" : "status-resolved"}">${g.open ? "Сработала" : "В норме"}</span>
          </span>
          <span class="alarm-bar"><i style="width:${Math.max(6, (g.count / max) * 100)}%"></i></span>
          <span class="inc-meta">${g.count} ${plural(g.count, "раз", "раза", "раз")} · последний ${fmtTime(g.last.started_at)} · ${esc(g.name)}</span>
        </button>
      </li>`).join("");
  }

  function renderList() {
    const ul = $("inc-list");
    const items = sorted().filter((i) => state.filter === "all" || i.status === state.filter);
    if (!items.length) {
      ul.innerHTML = `<li class="detail-empty">${state.filter === "open" ? "Открытых инцидентов нет. Всё работает." : "Инцидентов пока нет."}</li>`;
      return;
    }
    ul.innerHTML = items.map((i) => `
      <li class="inc-item">
        <button type="button" data-id="${esc(i.incident_id)}" aria-current="${i.incident_id === state.selectedId}">
          <span class="sev-dot sev-${esc(i.severity)}" title="${esc(SEVERITY[i.severity] || i.severity)}"></span>
          <span>
            <span class="inc-title">${esc(i.title)}</span><br>
            <span class="inc-meta">${fmtTime(i.started_at)} · ${esc(i.alarm_name)}</span>
          </span>
          <span class="status status-${esc(i.status)}">${esc(STATUS[i.status] || i.status)}</span>
        </button>
      </li>`).join("");
  }

  function colorLog(line) {
    const safe = esc(line);
    if (/\bERROR\b/.test(line)) return `<span class="lv-error">${safe}</span>`;
    if (/\bWARN\b/.test(line)) return `<span class="lv-warn">${safe}</span>`;
    return safe;
  }

  function renderDetail() {
    const box = $("detail");
    const i = state.incidents.find((x) => x.incident_id === state.selectedId);
    if (!i) {
      box.innerHTML = `<div class="detail-empty">Выберите инцидент слева, чтобы увидеть разбор.</div>`;
      return;
    }
    const s = i.stats || {};
    const num = (v) => (typeof v === "number" ? Math.round(v * 10) / 10 : v);
    const metric = (v, unit, label) => v == null ? "" : `<div class="metric"><b>${esc(num(v))}${unit}</b><span>${label}</span></div>`;
    const fix = i.auto_fix || {};
    const duration = i.resolved_at ? fmtDuration(new Date(i.resolved_at) - new Date(i.started_at)) : "ещё открыт";

    box.innerHTML = `
      <div class="detail-head">
        <h2>${esc(i.title)}</h2>
        <div class="detail-tags">
          <span class="status status-${esc(i.status)}">${esc(STATUS[i.status] || i.status)}</span>
          <span class="tag">${esc(SEVERITY[i.severity] || i.severity)}</span>
          <span>${fmtTime(i.started_at)}</span>
          <span>Длительность: ${duration}</span>
        </div>
      </div>
      <div class="detail-section">
        <h3>Что случилось</h3>
        <p>${esc(i.summary)}</p>
      </div>
      <div class="detail-section">
        <h3>Вероятная причина по мнению AI${i.ai_model ? " (" + esc(i.ai_model) + ")" : ""}</h3>
        <p class="ai-cause">${esc(i.root_cause)}</p>
      </div>
      <div class="detail-section">
        <h3>Что делать</h3>
        <ol class="reco">${(i.recommendations || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ol>
      </div>
      <div class="detail-section">
        <h3>Метрики в момент сбоя</h3>
        <div class="metric-row">
          ${metric(s.error_rate_percent, "%", "Ошибки 5xx")}
          ${metric(s.cpu_percent, "%", "CPU")}
          ${metric(s.disk_used_percent, "%", "Диск занят")}
          ${metric(s.p95_latency_ms, " мс", "Время ответа p95")}
          ${i.value != null ? `<div class="metric"><b>${esc(num(i.value))}</b><span>${esc(i.metric || "Значение метрики")}${i.threshold != null ? ", порог " + esc(i.threshold) : ""}</span></div>` : ""}
        </div>
      </div>
      <div class="detail-section">
        <h3>Автопочинка</h3>
        <div class="fix-box ${fix.applied ? "" : "is-off"}">
          ${fix.applied ? `Выполнено: <code>${esc(fix.action)}</code>. ` : ""}${esc(fix.result || "Не применялась")}
        </div>
      </div>
      ${i.log_summary.length ? `<div class="detail-section">
        <h3>Сводка логов</h3>
        <table class="log-table"><thead><tr><th>Уровень</th><th>Сообщение</th><th>Путь</th><th>Раз</th></tr></thead><tbody>
        ${i.log_summary.map((r) => `<tr><td class="lv-${String(r.level).toLowerCase()}">${esc(r.level)}</td><td>${esc(r.msg)}</td><td>${esc(r.path || "")}${r.status ? " · " + esc(r.status) : ""}</td><td>${esc(r.count)}</td></tr>`).join("")}
        </tbody></table>
      </div>` : ""}
      <div class="detail-section">
        <h3>Логи перед сбоем</h3>
        <pre class="logs">${(i.logs || []).map(colorLog).join("\n")}</pre>
        ${i.report_url ? `<p style="margin-top:12px"><a class="btn btn-ghost btn-small" href="${esc(i.report_url)}" target="_blank" rel="noopener">Открыть полный отчёт</a></p>` : ""}
      </div>`;
  }

  function select(id) {
    state.selectedId = id;
    history.replaceState(null, "", "#" + id);
    renderList();
    renderDetail();
    if (window.innerWidth < 960) $("detail").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderAll() {
    renderStats();
    renderChart();
    renderAlarms();
    renderList();
    renderDetail();
  }

  document.addEventListener("click", (e) => {
    const item = e.target.closest(".inc-item button");
    if (item) return select(item.dataset.id);
    const al = e.target.closest(".alarm-item button");
    if (al) return select(al.dataset.id);
    const chip = e.target.closest(".chip");
    if (chip) {
      state.filter = chip.dataset.filter;
      document.querySelectorAll(".chip").forEach((c) => c.setAttribute("aria-pressed", c === chip));
      return renderList();
    }
  });

  (async function init() {
    await loadIncidents();
    const fromHash = location.hash.slice(1);
    const first = sorted()[0];
    state.selectedId = state.incidents.some((i) => i.incident_id === fromHash) ? fromHash : first && first.incident_id;
    renderAll();
    if (cfg.API_URL && cfg.REFRESH_SECONDS > 0) {
      setInterval(async () => { await loadIncidents(); renderAll(); }, cfg.REFRESH_SECONDS * 1000);
    }
  })();
})();