#!/bin/bash
# Установка агента Kezekshi одной командой:
#   curl -sSL <адрес>/install.sh | sudo bash -s -- --key КЛЮЧ --name shop-api-prod
# Необязательно: --url <адрес /ingest>  --logs /путь/к/app.log,/другой.log
#               --health http://127.0.0.1:8080/health  (проверка, что приложение отвечает)
# Работает на Amazon Linux, Ubuntu, Debian, CentOS/RHEL с systemd.
# Обновление уже установленного агента без ключа: sudo bash install.sh --upgrade [--health URL]
#   (ключ, имя, адрес и логи берутся из /etc/kezekshi/agent.env)
set -euo pipefail

KEY=""; NAME="$(hostname)"; URL="${KEZEKSHI_DEFAULT_INGEST_URL:-}"; LOGS=""; HEALTH=""; UPGRADE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --key)  KEY="$2";  shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --url)  URL="$2";  shift 2 ;;
    --logs) LOGS="$2"; shift 2 ;;
    --health) HEALTH="$2"; shift 2 ;;
    --upgrade) UPGRADE=1; shift ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "Неизвестный параметр: $1" >&2; exit 1 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then echo "Запустите через sudo" >&2; exit 1; fi
if [ "$UPGRADE" = 1 ]; then
  [ -f /etc/kezekshi/agent.env ] || { echo "Агент ещё не установлен, нужен --key" >&2; exit 1; }
  old() { grep "^$1=" /etc/kezekshi/agent.env | head -1 | cut -d= -f2- || true; }
  KEY="$(old KEZEKSHI_KEY)"; NAME="$(old KEZEKSHI_SERVER)"; URL="$(old KEZEKSHI_INGEST_URL)"
  LOGS="${LOGS:-$(old KEZEKSHI_LOG_FILES)}"; HEALTH="${HEALTH:-$(old KEZEKSHI_HEALTH_URL)}"
fi
if [ -z "$KEY" ]; then echo "Нужен ключ: --key КЛЮЧ" >&2; exit 1; fi
if ! command -v systemctl >/dev/null; then echo "Нужен systemd" >&2; exit 1; fi
case "$NAME" in *[!A-Za-z0-9._-]*) echo "Имя сервера: только буквы, цифры, точка, _ и -" >&2; exit 1 ;; esac

# Python 3, если его нет
if ! command -v python3 >/dev/null; then
  if command -v dnf >/dev/null; then dnf install -y python3
  elif command -v yum >/dev/null; then yum install -y python3
  elif command -v apt-get >/dev/null; then apt-get update -y && apt-get install -y python3
  else echo "Установите python3 вручную" >&2; exit 1; fi
fi
PY="$(command -v python3)"

# Если логи не указаны, берём то, что найдётся
if [ -z "$LOGS" ]; then
  for f in /var/log/demoapp/app.log /var/log/nginx/error.log /var/log/apache2/error.log /var/log/httpd/error_log; do
    [ -f "$f" ] && LOGS="${LOGS:+$LOGS,}$f"
  done
fi

mkdir -p /opt/kezekshi /etc/kezekshi
cat > /opt/kezekshi/kezekshi_agent.py <<'KZ_AGENT_EOF'
#!/usr/bin/env python3
"""Kezekshi agent v2: раз в минуту собирает CPU, память, диск, новые строки ERROR
и статистику логов, проверяет, отвечает ли приложение, и отправляет всё JSON-ом на /ingest.
Только стандартная библиотека Python.

Настройки из переменных окружения (файл /etc/kezekshi/agent.env):
  KEZEKSHI_KEY        ключ сервера (выдаёт /register)
  KEZEKSHI_SERVER     имя сервера, например shop-api-prod
  KEZEKSHI_INGEST_URL адрес /ingest; пусто = режим проверки, JSON печатается в консоль
  KEZEKSHI_LOG_FILES  пути к логам через запятую (необязательно)
  KEZEKSHI_INTERVAL   период в секундах (по умолчанию 60)
  KEZEKSHI_HEALTH_URL адрес приложения для проверки (необязательно), например
                      http://127.0.0.1:8080/health. Проверяется 3 раза за период.
"""
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

KEY = os.environ.get("KEZEKSHI_KEY", "")
SERVER = os.environ.get("KEZEKSHI_SERVER", os.uname().nodename)
INGEST_URL = os.environ.get("KEZEKSHI_INGEST_URL", "")
LOG_FILES = [p.strip() for p in os.environ.get("KEZEKSHI_LOG_FILES", "").split(",") if p.strip()]
INTERVAL = int(os.environ.get("KEZEKSHI_INTERVAL", "60"))
HEALTH_URL = os.environ.get("KEZEKSHI_HEALTH_URL", "").strip()
HEALTH_TIMEOUT = 5.0
PROBES = 3
MAX_ERRORS = 20
ERROR_RE = re.compile(r"\b(ERROR|CRITICAL|FATAL|Traceback)\b|\"level\":\s*\"ERROR\"")
# код ответа: JSON-лог ("status": 401) или access-лог nginx/apache ("GET / HTTP/1.1" 401 ...)
STATUS_RE = re.compile(r"\"status\":\s*(\d{3})\b|\"\s(\d{3})\s")
LATENCY_KEYS = ("latency_ms", "duration_ms", "response_time_ms", "elapsed_ms")
# убираем личные данные перед отправкой
SECRET_RES = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
    (re.compile(r"\+?\d[\d\s()-]{9,}\d"), "<phone>"),
    (re.compile(r"(?i)(token|password|secret|key)[\"']?\s*[:=]\s*[\"']?[^\s\"',]+"), r"\1=<hidden>"),
]


def cpu_times():
    with open("/proc/stat") as f:
        parts = [int(x) for x in f.readline().split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    return idle, sum(parts)


def cpu_percent(prev):
    idle, total = cpu_times()
    d_idle, d_total = idle - prev[0], total - prev[1]
    pct = 0.0 if d_total <= 0 else 100.0 * (1 - d_idle / d_total)
    return round(pct, 1), (idle, total)


def mem_percent():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            name, value = line.split(":", 1)
            info[name] = int(value.split()[0])
    total = info["MemTotal"]
    available = info.get("MemAvailable", info.get("MemFree", 0))
    return round(100.0 * (total - available) / total, 1)


def disk_percent(path="/"):
    total, used, _ = shutil.disk_usage(path)
    return round(100.0 * used / total, 1)


def clean(line):
    for pattern, repl in SECRET_RES:
        line = pattern.sub(repl, line)
    return line[:500]


class LogTail:
    """Читает только новые строки лога, начиная с конца файла при запуске."""

    def __init__(self, path):
        self.path = path
        self.pos = os.path.getsize(path) if os.path.exists(path) else 0

    def new_lines(self):
        if not os.path.exists(self.path):
            return []
        size = os.path.getsize(self.path)
        if size < self.pos:  # лог обрезали или повернули
            self.pos = 0
        with open(self.path, errors="replace") as f:
            f.seek(self.pos)
            data = f.read()
            self.pos = f.tell()
        return data.splitlines()


def log_stats(lines):
    """Сколько строк, сколько 401/403 и 404, задержка p95 (если лог её пишет)."""
    auth = nf = 0
    lat = []
    for line in lines:
        m = STATUS_RE.search(line)
        if m:
            code = int(m.group(1) or m.group(2))
            auth += code in (401, 403)
            nf += code == 404
        if line.startswith("{") and "_ms" in line:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            for k in LATENCY_KEYS:
                v = rec.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    lat.append(float(v))
                    break
    p95 = None
    if lat:
        lat.sort()
        p95 = round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 1)
    return {"log_lines": len(lines), "auth_failures": auth, "not_found": nf, "latency_p95_ms": p95}


def probe():
    """(ok, мс). ok = приложение ответило кодом ниже 500 за HEALTH_TIMEOUT."""
    t = time.time()
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=HEALTH_TIMEOUT) as r:
            r.read(65536)
            ok = r.status < 500
    except urllib.error.HTTPError as e:
        ok = e.code < 500
    except Exception:
        ok = False
    return ok, round((time.time() - t) * 1000, 1)


def send(payload):
    body = json.dumps(payload).encode()
    if not INGEST_URL:
        print(body.decode(), flush=True)
        return True
    req = urllib.request.Request(
        INGEST_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Kezekshi-Key": KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # сеть упала: просто пробуем в следующий раз
        print(f"send failed: {exc}", file=sys.stderr, flush=True)
        return False


def main():
    if INGEST_URL and not KEY:
        sys.exit("KEZEKSHI_KEY is required when KEZEKSHI_INGEST_URL is set")
    tails = [LogTail(p) for p in LOG_FILES]
    prev = cpu_times()
    time.sleep(1)
    while True:
        # за период: несколько проверок приложения через равные промежутки
        probes = []
        for i in range(PROBES if HEALTH_URL else 1):
            if HEALTH_URL:
                probes.append(probe())
            if not os.environ.get("KEZEKSHI_ONCE"):
                time.sleep(INTERVAL / (PROBES if HEALTH_URL else 1))
        cpu, prev = cpu_percent(prev)
        lines = []
        for t in tails:
            lines += t.new_lines()
        errors = [clean(l) for l in lines if ERROR_RE.search(l)]
        payload = {
            "key": KEY,
            "server": SERVER,
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "agent_version": 2,
            "cpu": cpu,
            "mem": mem_percent(),
            "disk": disk_percent(),
            "errors": errors[-MAX_ERRORS:],
            "error_count": len(errors),
        }
        payload.update(log_stats(lines) if tails else {})
        if probes:
            ok = [p for p in probes if p[0]]
            payload["health_ok"] = 1 if len(ok) * 2 > len(probes) else 0
            # время ответа: медиана удачных проверок; если все упали, считаем таймаут
            times = sorted(p[1] for p in ok) or [HEALTH_TIMEOUT * 1000]
            payload["response_ms"] = times[len(times) // 2]
        send(payload)
        if os.environ.get("KEZEKSHI_ONCE"):
            return


if __name__ == "__main__":
    main()
KZ_AGENT_EOF
chmod 755 /opt/kezekshi/kezekshi_agent.py

cat > /etc/kezekshi/agent.env <<EOF
KEZEKSHI_KEY=$KEY
KEZEKSHI_SERVER=$NAME
KEZEKSHI_INGEST_URL=$URL
KEZEKSHI_LOG_FILES=$LOGS
KEZEKSHI_INTERVAL=60
KEZEKSHI_HEALTH_URL=$HEALTH
EOF
chmod 600 /etc/kezekshi/agent.env

cat > /etc/systemd/system/kezekshi-agent.service <<EOF
[Unit]
Description=Kezekshi monitoring agent
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/kezekshi/agent.env
ExecStart=$PY /opt/kezekshi/kezekshi_agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable kezekshi-agent >/dev/null
systemctl restart kezekshi-agent
sleep 2

if systemctl is-active --quiet kezekshi-agent; then
  echo "Kezekshi: агент запущен на сервере $NAME"
  echo "Логи: ${LOGS:-не найдены, укажите --logs}"
  echo "Проверка приложения: ${HEALTH:-выключена (можно включить: --health URL)}"
  [ -z "$URL" ] && echo "Режим проверки: адрес /ingest не задан, данные печатаются в journalctl -u kezekshi-agent"
  echo "Первые метрики появятся через минуту."
else
  echo "Агент не запустился, смотрите: journalctl -u kezekshi-agent -n 20" >&2
  exit 1
fi
