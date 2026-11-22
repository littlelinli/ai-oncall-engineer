"""Сборщик датасета ShopSim.

Раз в минуту складывает в CSV одну строку: что происходило с сайтом
(запросы, ошибки, задержки по страницам), с сервером (CPU, память, диск, сеть)
и какая проблема была включена на самом деле (метки от traffic.py).
Это готовый датасет для прогноза нагрузки и классификации причин.

Ещё он выполняет две «служебные» задачи сервера:
1. раз в час удаляет старые загрузки (если очистка не «сломана» сценарием);
2. запускает фоновую задачу, которая грузит CPU (сценарий cpu_hog).

Файлы: <SIM_DATA>/dataset/metrics-YYYY-MM-DD.csv (UTC).
Если задан SIM_S3_BUCKET, раз в час копирует датасет и events.jsonl в S3.
"""
import csv
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

DATA_DIR = os.environ.get("SIM_DATA", "/var/lib/shopsim")
LOG_PATH = os.environ.get("SIM_LOG", "/var/log/demoapp/app.log")
INTERVAL = float(os.environ.get("SIM_COLLECT_INTERVAL", "60"))
CLEANUP_EVERY = float(os.environ.get("SIM_CLEANUP_EVERY", "3600"))
UPLOAD_MAX_AGE = float(os.environ.get("SIM_UPLOAD_MAX_AGE", "21600"))  # 6 часов
S3_BUCKET = os.environ.get("SIM_S3_BUCKET", "").strip()
DATASET_DIR = os.path.join(DATA_DIR, "dataset")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LABELS_PATH = os.path.join(DATA_DIR, "labels.json")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
ENDPOINTS = ["/", "/catalog", "/item/<int:lot_id>", "/search", "/bid", "/checkout", "/login", "/upload"]
SHORT = {"/": "home", "/catalog": "catalog", "/item/<int:lot_id>": "item", "/search": "search",
         "/bid": "bid", "/checkout": "checkout", "/login": "login", "/upload": "upload"}

COLUMNS = (["time", "requests", "rps", "status_2xx", "status_4xx", "status_5xx", "error_rate",
            "latency_p50_ms", "latency_p95_ms", "latency_p99_ms", "latency_max_ms", "queue_p95_ms", "inflight_max",
            "unauthorized", "not_found", "debug_lines"]
           + [f"req_{SHORT[e]}" for e in ENDPOINTS]
           + [f"p95_{SHORT[e]}" for e in ENDPOINTS]
           + ["cpu_pct", "cpu_steal_pct", "mem_pct", "swap_pct", "disk_pct", "load1",
              "net_rx_kb", "net_tx_kb", "procs",
              "flash_multiplier", "incident", "incident_severity", "incident_id", "flash_id"])


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_state():
    return read_json(STATE_PATH, {})


# ---------------- системные метрики ----------------

def cpu_times():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    steal = v[7] if len(v) > 7 else 0
    return idle, steal, sum(v[:8])


def mem_stats():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, val = line.split(":", 1)
            info[k] = int(val.split()[0])
    total = info["MemTotal"]
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    swap_total = info.get("SwapTotal", 0)
    swap_used = swap_total - info.get("SwapFree", 0)
    return (round(100 * (total - avail) / total, 1),
            round(100 * swap_used / swap_total, 1) if swap_total else 0.0)


def net_bytes():
    rx = tx = 0
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, data = line.split(":", 1)
            if name.strip() == "lo":
                continue
            parts = data.split()
            rx += int(parts[0])
            tx += int(parts[8])
    return rx, tx


def procs():
    return sum(1 for p in os.listdir("/proc") if p.isdigit())


# ---------------- журнал приложения ----------------

class LogTail:
    def __init__(self, path):
        self.path = path
        self.pos = os.path.getsize(path) if os.path.exists(path) else 0
        self.buf = ""

    def lines(self):
        if not os.path.exists(self.path):
            return []
        size = os.path.getsize(self.path)
        if size < self.pos:  # logrotate copytruncate
            self.pos = 0
        with open(self.path, errors="replace") as f:
            f.seek(self.pos)
            data = f.read()
            self.pos = f.tell()
        data = self.buf + data
        parts = data.split("\n")
        self.buf = parts.pop()  # недописанная строка
        return parts


def pct(values, q):
    if not values:
        return 0.0
    values = sorted(values)
    i = min(len(values) - 1, max(0, int(round(q / 100 * (len(values) - 1)))))
    return round(values[i], 1)


def aggregate(lines, seconds):
    lat, queue, per_ep, inflight = [], [], {e: [] for e in ENDPOINTS}, 0
    s2 = s4 = s5 = unauth = nf = debug = 0
    for raw in lines:
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("level") == "DEBUG":
            debug += 1
            continue
        if "status" not in r or "latency_ms" not in r:
            continue
        st, ms = int(r["status"]), float(r["latency_ms"])
        lat.append(ms)
        queue.append(float(r.get("queue_ms", 0)))
        inflight = max(inflight, int(r.get("inflight", 0)))
        if st >= 500:
            s5 += 1
        elif st >= 400:
            s4 += 1
            unauth += st == 401
            nf += st == 404
        else:
            s2 += 1
        if r.get("path") in per_ep:
            per_ep[r["path"]].append(ms)
    n = len(lat)
    row = {"requests": n, "rps": round(n / seconds, 3), "status_2xx": s2, "status_4xx": s4,
           "status_5xx": s5, "error_rate": round(s5 / n, 4) if n else 0.0,
           "latency_p50_ms": pct(lat, 50), "latency_p95_ms": pct(lat, 95),
           "latency_p99_ms": pct(lat, 99), "latency_max_ms": round(max(lat), 1) if lat else 0.0,
           "queue_p95_ms": pct(queue, 95),
           "inflight_max": inflight, "unauthorized": unauth, "not_found": nf, "debug_lines": debug}
    for e in ENDPOINTS:
        row[f"req_{SHORT[e]}"] = len(per_ep[e])
        row[f"p95_{SHORT[e]}"] = pct(per_ep[e], 95)
    return row


# ---------------- служебные задачи сервера ----------------

def cleanup_loop():
    while True:
        time.sleep(CLEANUP_EVERY)
        if not load_state().get("uploads_cleanup", True):
            continue
        cutoff = time.time() - UPLOAD_MAX_AGE
        removed = 0
        try:
            for name in os.listdir(UPLOAD_DIR):
                p = os.path.join(UPLOAD_DIR, name)
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    removed += 1
        except OSError:
            pass
        print(f"cleanup: removed {removed} old uploads", flush=True)


def cpu_hog_loop():
    """Фоновая «задача» (бэкап, пересчёт отчётов) грузит CPU на заданный процент.
    Отдельные процессы, чтобы нагрузка была на всех ядрах."""
    workers = []
    current = 0
    while True:
        target = int(load_state().get("cpu_hog_pct") or 0)
        if target != current:
            for p in workers:
                p.terminate()
            workers = []
            if target > 0:
                code = ("import time\nwhile True:\n t=time.time()\n"
                        f" while time.time()-t<{target / 1000:.3f}: pass\n"
                        f" time.sleep({(100 - target) / 1000:.3f})\n")
                for _ in range(os.cpu_count() or 1):
                    workers.append(subprocess.Popen(["nice", "-n", "5", "python3", "-c", code]))
            current = target
            print(f"cpu_hog: {target}%", flush=True)
        time.sleep(5)


def s3_sync_loop():
    while True:
        time.sleep(3600)
        for src, dst in ((DATASET_DIR, "dataset"),):
            subprocess.run(["aws", "s3", "sync", src, f"s3://{S3_BUCKET}/shopsim/{dst}/", "--only-show-errors"])
        events = os.path.join(DATA_DIR, "events.jsonl")
        if os.path.exists(events):
            subprocess.run(["aws", "s3", "cp", events, f"s3://{S3_BUCKET}/shopsim/events.jsonl", "--only-show-errors"])


# ---------------- главный цикл ----------------

def write_row(row):
    os.makedirs(DATASET_DIR, exist_ok=True)
    day = row["time"][:10]
    path = os.path.join(DATASET_DIR, f"metrics-{day}.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerow(row)


def main():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    threading.Thread(target=cleanup_loop, daemon=True).start()
    threading.Thread(target=cpu_hog_loop, daemon=True).start()
    if S3_BUCKET:
        threading.Thread(target=s3_sync_loop, daemon=True).start()
    tail = LogTail(LOG_PATH)
    prev_cpu, prev_net = cpu_times(), net_bytes()
    t_prev = time.time()
    while True:
        # ровно по границе интервала, чтобы строки шли ровным шагом
        time.sleep(INTERVAL - (time.time() % INTERVAL) if INTERVAL >= 1 else INTERVAL)
        now = time.time()
        seconds = max(now - t_prev, 1e-3)
        t_prev = now
        row = {"time": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")}
        row.update(aggregate(tail.lines(), seconds))
        cpu = cpu_times()
        d_total = max(cpu[2] - prev_cpu[2], 1)
        row["cpu_pct"] = round(100 * (1 - (cpu[0] - prev_cpu[0]) / d_total), 1)
        row["cpu_steal_pct"] = round(100 * (cpu[1] - prev_cpu[1]) / d_total, 1)
        prev_cpu = cpu
        row["mem_pct"], row["swap_pct"] = mem_stats()
        total, used, _ = shutil.disk_usage("/")
        row["disk_pct"] = round(100 * used / total, 1)
        row["load1"] = round(os.getloadavg()[0], 2)
        net = net_bytes()
        row["net_rx_kb"] = round((net[0] - prev_net[0]) / 1024, 1)
        row["net_tx_kb"] = round((net[1] - prev_net[1]) / 1024, 1)
        prev_net = net
        row["procs"] = procs()
        lb = read_json(LABELS_PATH, {})
        row["flash_multiplier"] = lb.get("flash_multiplier", 1.0)
        row["flash_id"] = lb.get("flash_id") or ""
        row["incident"] = lb.get("incident") or "none"
        row["incident_severity"] = lb.get("incident_severity") or ""
        row["incident_id"] = lb.get("incident_id") or ""
        write_row(row)


if __name__ == "__main__":
    main()
