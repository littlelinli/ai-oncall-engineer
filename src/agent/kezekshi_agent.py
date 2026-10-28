#!/usr/bin/env python3
"""Kezekshi agent: раз в минуту собирает CPU, память, диск и новые строки ERROR
из лога и отправляет их JSON-ом на /ingest. Только стандартная библиотека Python.

Настройки из переменных окружения (файл /etc/kezekshi/agent.env):
  KEZEKSHI_KEY        ключ сервера (выдаёт /register)
  KEZEKSHI_SERVER     имя сервера, например shop-api-prod
  KEZEKSHI_INGEST_URL адрес /ingest; пусто = режим проверки, JSON печатается в консоль
  KEZEKSHI_LOG_FILES  пути к логам через запятую (необязательно)
  KEZEKSHI_INTERVAL   период в секундах (по умолчанию 60)
"""
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from datetime import datetime, timezone

KEY = os.environ.get("KEZEKSHI_KEY", "")
SERVER = os.environ.get("KEZEKSHI_SERVER", os.uname().nodename)
INGEST_URL = os.environ.get("KEZEKSHI_INGEST_URL", "")
LOG_FILES = [p.strip() for p in os.environ.get("KEZEKSHI_LOG_FILES", "").split(",") if p.strip()]
INTERVAL = int(os.environ.get("KEZEKSHI_INTERVAL", "60"))
MAX_ERRORS = 20
ERROR_RE = re.compile(r"\b(ERROR|CRITICAL|FATAL|Traceback)\b|\"level\":\s*\"ERROR\"")
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
        cpu, prev = cpu_percent(prev)
        errors = []
        for t in tails:
            errors += [clean(l) for l in t.new_lines() if ERROR_RE.search(l)]
        payload = {
            "key": KEY,
            "server": SERVER,
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cpu": cpu,
            "mem": mem_percent(),
            "disk": disk_percent(),
            "errors": errors[-MAX_ERRORS:],
            "error_count": len(errors),
        }
        send(payload)
        if os.environ.get("KEZEKSHI_ONCE"):
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
