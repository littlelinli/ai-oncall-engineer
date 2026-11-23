"""Генератор посетителей и проблем для ShopSim.

Что делает:
1. Посетители приходят с реальным ритмом: ночью мало, днём больше, вечером пик,
   в пятницу вечером и в выходные ещё больше, плюс случайный шум.
2. Каждый посетитель проходит путь по сайту: главная, каталог, лот, поиск,
   иногда ставка, оплата, вход, загрузка фото. Между шагами думает.
3. Иногда случается наплыв (аукцион заканчивается, акция): в 2–12 раз больше
   людей на 10–25 минут. Маленький наплыв проходит без ошибок, большой роняет сайт.
4. Иногда начинается проблема: медленная оплата, плохой релиз, утечка памяти,
   поток логов, сломанная очистка диска, атака ботов, фоновая задача грузит CPU.
   Сила проблемы случайна: часть заканчивается тревогой, часть нет.
5. Всё, что генератор включил, пишется в events.jsonl (истинная причина),
   а текущее состояние в labels.json: это разметка для обучения модели.

Профили (SIM_PROFILE):
  shop    интернет-магазин/аукцион: вечерний пик, выходные выше, частые наплывы
  office  рабочий сервис для компаний (CRM): пик в рабочие часы, обеденный провал,
          в выходные почти пусто, ночной бэкап грузит CPU (это норма, не проблема),
          число пользователей растёт каждую неделю, релизы только в рабочее время,
          наплывы редкие и небольшие (конец месяца, отчёты)

Запуск: python traffic.py  (настройки через переменные окружения, см. CONFIG)
Для проверки можно ускорить время: SIM_SPEED=60 (час проходит за минуту).
"""
import json
import math
import os
import random
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("SIM_TZ", "Asia/Almaty"))
except Exception:  # нет базы часовых поясов
    TZ = timezone(timedelta(hours=5))

CONFIG = {
    "target": os.environ.get("SIM_TARGET", "http://127.0.0.1:8080").rstrip("/"),
    "base_sessions": float(os.environ.get("SIM_BASE_SESSIONS", "0.8")),  # посетителей/сек в пик дня
    "max_workers": int(os.environ.get("SIM_MAX_WORKERS", "120")),
    "speed": float(os.environ.get("SIM_SPEED", "1")),
    "incidents": os.environ.get("SIM_INCIDENTS", "1") == "1",
    "incident_every_h": float(os.environ.get("SIM_INCIDENT_EVERY_H", "3")),  # в среднем раз в N часов
    "flash_every_h": float(os.environ.get("SIM_FLASH_EVERY_H", "2.5")),
    "data_dir": os.environ.get("SIM_DATA", "/var/lib/shopsim"),
    "token": os.environ.get("DEMO_TOKEN", ""),
    "seed": os.environ.get("SIM_SEED"),
    "profile": os.environ.get("SIM_PROFILE", "shop").strip().lower(),
    # рост числа пользователей в неделю (0.05 = +5%), считается от первого запуска
    "growth_per_week": float(os.environ.get("SIM_GROWTH_PER_WEEK", "0")),
    "mega_flash_p": float(os.environ.get("SIM_MEGA_FLASH_P", "0.15")),  # доля больших наплывов
}
OFFICE = CONFIG["profile"] == "office"

rnd = random.Random(int(CONFIG["seed"])) if CONFIG["seed"] else random.Random()
SPEED = max(CONFIG["speed"], 1e-6)
T0_REAL = time.time()
T0_SIM = time.time()
EVENTS_PATH = os.path.join(CONFIG["data_dir"], "events.jsonl")
LABELS_PATH = os.path.join(CONFIG["data_dir"], "labels.json")

labels = {"flash_multiplier": 1.0, "flash_id": None, "incident": None,
          "incident_severity": None, "incident_id": None}
labels_lock = threading.Lock()
stats = {"sessions": 0, "dropped": 0, "requests": 0, "client_errors": 0, "timeouts": 0}
stats_lock = threading.Lock()


# ---------------- время ----------------

def sim_now():
    """Время в симуляции (при SPEED=1 совпадает с реальным)."""
    return T0_SIM + (time.time() - T0_REAL) * SPEED


def sim_sleep(sim_seconds):
    time.sleep(max(0.0, sim_seconds / SPEED))


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


# ---------------- ритм нагрузки ----------------

def office_daily(hour):
    """0..1: ночью почти никого, вход сотрудников в 9, обеденный провал 13–14, к 18 спад."""
    night = 0.03
    work = 0.85 / (1 + math.exp(-(hour - 8.8) * 3)) / (1 + math.exp((hour - 18.3) * 2))
    login_spike = 0.25 * math.exp(-((hour - 9.2) / 0.4) ** 2)
    lunch_dip = 0.3 * math.exp(-((hour - 13.5) / 0.6) ** 2)
    evening = 0.08 * math.exp(-((hour - 21) / 1.5) ** 2)
    return max(0.0, min(1.0, night + work + login_spike - lunch_dip + evening))


def office_weekly(dt):
    wd, h = dt.weekday(), dt.hour
    if wd == 5:
        return 0.15
    if wd == 6:
        return 0.1
    if wd == 0 and h < 12:            # понедельник утром все разбирают почту и задачи
        return 1.2
    if wd == 4 and h >= 15:           # пятница после обеда
        return 0.75
    return 1.0


def daily_curve(hour):
    """0..1: ночью ~0.08, утренний подъём, обеденный горб, вечерний пик в 20–21."""
    if OFFICE:
        return office_daily(hour)
    night = 0.08
    morning = 0.55 * math.exp(-((hour - 12.5) / 3.2) ** 2)
    evening = 0.95 * math.exp(-((hour - 20.5) / 2.3) ** 2)
    return min(1.0, night + morning + evening)


def weekly_factor(dt):
    if OFFICE:
        return office_weekly(dt)
    wd, h = dt.weekday(), dt.hour
    if wd == 4 and h >= 17:          # пятница вечер
        return 1.45
    if wd in (5, 6):                  # выходные
        return 1.25 if 11 <= h <= 23 else 1.05
    if wd == 0:                       # понедельник
        return 0.85
    return 1.0


class Noise:
    """Медленно меняющийся шум (ничего не бывает идеально ровным)."""

    def __init__(self):
        self.v = 1.0

    def step(self):
        self.v += (1.0 - self.v) * 0.02 + rnd.gauss(0, 0.03)
        self.v = min(1.4, max(0.7, self.v))
        return self.v


# ---------------- события и разметка ----------------

def write_event(kind, phase, **extra):
    row = {"time": iso(sim_now()), "real_time": iso(time.time()), "kind": kind, "phase": phase}
    row.update(extra)
    with open(EVENTS_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")
    print("event:", json.dumps(row), flush=True)


def save_labels():
    with labels_lock:
        data = dict(labels)
    data["updated"] = iso(time.time())
    tmp = LABELS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, LABELS_PATH)


def set_state(changes):
    req = urllib.request.Request(
        CONFIG["target"] + "/sim/state", data=json.dumps(changes).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Demo-Token": CONFIG["token"]})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as exc:
        print("set_state failed:", exc, flush=True)
        return False


def get_state():
    req = urllib.request.Request(CONFIG["target"] + "/sim/state",
                                 headers={"X-Demo-Token": CONFIG["token"]})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


# ---------------- посетители ----------------

SEARCH_TERMS = ["lamp", "watch", "vintage", "camera", "guitar", "phone", "laptop", "coin",
                "sneakers", "vinyl", "drone", "ring", "chair", "console", "headphones"]


def hit(method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-Request-Start": f"{time.time():.3f}"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(CONFIG["target"] + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = 0
    with stats_lock:
        stats["requests"] += 1
        if code == 0:
            stats["timeouts"] += 1
        elif code >= 500:
            stats["client_errors"] += 1
    return code


def think():
    sim_sleep(rnd.uniform(1.0, 6.0))


def office_session(rush):
    """Сотрудник компании: входит, ищет клиентов и сделки, открывает карточки, загружает файлы.
    rush: конец месяца, все закрывают отчёты."""
    user = f"emp{rnd.randint(1, 3000)}"
    hit("GET", "/")
    think()
    if rnd.random() < 0.6:
        hit("POST", "/login", {"user": user, "password": "correct-horse"})
        think()
    for _ in range(rnd.randint(2, 6)):
        if rnd.random() < 0.6:
            hit("GET", "/search?q=" + rnd.choice(SEARCH_TERMS))
        else:
            hit("GET", f"/catalog?page={rnd.randint(0, 30)}")
        think()
    for _ in range(rnd.randint(1, 4)):
        hit("GET", f"/item/{rnd.randint(1, 20000)}")
        think()
    if rnd.random() < (0.25 if rush else 0.1):
        hit("POST", "/upload", {"user": user})
    if rnd.random() < 0.03:
        hit("POST", "/checkout", {"user": user}, timeout=12)


def session(auction_heavy):
    """Один посетитель. auction_heavy: во время наплыва люди в основном делают ставки."""
    if OFFICE:
        return office_session(auction_heavy)
    user = f"u{rnd.randint(1, 50000)}"
    hit("GET", "/")
    think()
    if rnd.random() < 0.15 or auction_heavy and rnd.random() < 0.3:
        hit("POST", "/login", {"user": user, "password": "correct-horse"})
        think()
    for _ in range(rnd.randint(1, 4)):
        if rnd.random() < 0.45:
            hit("GET", "/search?q=" + rnd.choice(SEARCH_TERMS))
        else:
            hit("GET", f"/catalog?page={rnd.randint(0, 30)}")
        think()
    hot_lot = rnd.randint(1, 60) if auction_heavy else rnd.randint(1, 20000)
    for _ in range(rnd.randint(1, 3)):
        hit("GET", f"/item/{hot_lot if rnd.random() < 0.7 else rnd.randint(1, 20000)}")
        think()
    bid_p = 0.85 if auction_heavy else 0.18
    if rnd.random() < bid_p:
        for _ in range(rnd.randint(1, 5 if auction_heavy else 2)):
            hit("POST", "/bid", {"lot_id": hot_lot, "user": user})
            sim_sleep(rnd.uniform(0.5, 3.0))
    if rnd.random() < 0.08:
        hit("POST", "/checkout", {"user": user}, timeout=12)
    if rnd.random() < 0.03:
        hit("POST", "/upload", {"user": user})


# ---------------- наплывы ----------------

def flash_loop():
    while True:
        sim_sleep(rnd.expovariate(1 / (CONFIG["flash_every_h"] * 3600)))
        mega = rnd.random() < CONFIG["mega_flash_p"]
        peak = rnd.uniform(6, 12) if mega else rnd.uniform(1.8, 4.5)
        ramp_up, plateau, ramp_down = rnd.uniform(120, 300), rnd.uniform(300, 900), rnd.uniform(180, 420)
        fid = f"flash-{int(sim_now())}"
        write_event("flash_crowd", "start", id=fid, peak_multiplier=round(peak, 2),
                    duration_s=int(ramp_up + plateau + ramp_down), mega=mega)
        start = sim_now()
        while True:
            t = sim_now() - start
            if t < ramp_up:
                m = 1 + (peak - 1) * t / ramp_up
            elif t < ramp_up + plateau:
                m = peak
            elif t < ramp_up + plateau + ramp_down:
                m = peak - (peak - 1) * (t - ramp_up - plateau) / ramp_down
            else:
                break
            with labels_lock:
                labels.update(flash_multiplier=round(m, 2), flash_id=fid)
            sim_sleep(5)
        with labels_lock:
            labels.update(flash_multiplier=1.0, flash_id=None)
        write_event("flash_crowd", "end", id=fid)


# ---------------- проблемы (инциденты) ----------------

def bot_attack(stop_at, rate):
    """Перебор паролей и сканирование адресов."""
    paths = ["/wp-login.php", "/.env", "/admin", "/phpmyadmin", "/api/v1/users", "/.git/config"]
    with ThreadPoolExecutor(max_workers=12) as pool:
        while sim_now() < stop_at:
            for _ in range(max(1, int(rnd.gauss(rate, rate * 0.2)))):
                if rnd.random() < 0.7:
                    pool.submit(hit, "POST", "/login", {"user": "admin", "password": f"p{rnd.randint(0, 10**6)}"})
                else:
                    pool.submit(hit, "GET", rnd.choice(paths))
            sim_sleep(1)


SCENARIOS = {
    # имя: (вес, функция, которая возвращает (длительность_с, изменения_состояния, параметры))
    "payment_slow": 3,
    "bad_deploy": 3,
    "memory_leak": 2,
    "log_storm": 2,
    "cleanup_broken": 1,
    "bot_attack": 2,
    "cpu_hog": 2,
    "app_down": 1,
}
if OFFICE:
    # в рабочем сервисе: чаще утечки, диск и релизы, реже атаки и медленная оплата
    SCENARIOS = {"payment_slow": 1, "bad_deploy": 3, "memory_leak": 3, "log_storm": 2,
                 "cleanup_broken": 2, "bot_attack": 1, "cpu_hog": 1, "app_down": 1}


def work_hours(dt):
    return dt.weekday() < 5 and 10 <= dt.hour < 17


def plan(name):
    strong = rnd.random() < 0.45
    sev = "strong" if strong else "mild"
    if name == "payment_slow":
        lat = rnd.uniform(3.0, 7.0) if strong else rnd.uniform(0.8, 2.0)
        return rnd.uniform(600, 2400), {"payment_latency": round(lat, 2)}, sev
    if name == "bad_deploy":
        rate = rnd.uniform(0.3, 0.8) if strong else rnd.uniform(0.03, 0.1)
        ch = {"checkout_error_rate": round(rate, 3)}
        if strong:
            ch["search_error_rate"] = round(rate / 3, 3)
        return rnd.uniform(300, 1800), ch, sev
    if name == "memory_leak":
        rate = rnd.uniform(6, 12) if strong else rnd.uniform(1, 3)
        return rnd.uniform(1800, 5400), {"leak_mb_per_min": round(rate, 1)}, sev
    if name == "log_storm":
        n = rnd.randint(15, 40) if strong else rnd.randint(2, 6)
        return rnd.uniform(1200, 3600), {"log_storm": n}, sev
    if name == "cleanup_broken":
        return rnd.uniform(7200, 6 * 3600), {"uploads_cleanup": False}, sev
    if name == "bot_attack":
        rate = rnd.uniform(12, 25) if strong else rnd.uniform(2, 5)
        return rnd.uniform(300, 1200), {"_bot_rate": rate}, sev
    if name == "cpu_hog":
        pct = rnd.randint(85, 100) if strong else rnd.randint(30, 55)
        return rnd.uniform(600, 2400), {"cpu_hog_pct": pct}, sev
    if name == "app_down":
        # приложение «упало» и само поднимается: лёгкий случай 1–2 минуты (аларм может не успеть), сильный 3–8
        duration = rnd.uniform(180, 480) if strong else rnd.uniform(60, 120)
        return duration, {"app_down_until": round(time.time() + duration / SPEED, 1)}, sev
    raise ValueError(name)


RESET = {"payment_latency": 0.15, "checkout_error_rate": 0.0, "search_error_rate": 0.0,
         "leak_mb_per_min": 0.0, "log_storm": 0, "uploads_cleanup": True, "cpu_hog_pct": 0,
         "app_down_until": 0}


def incident_loop():
    names = list(SCENARIOS)
    weights = [SCENARIOS[n] for n in names]
    sim_sleep(rnd.uniform(1200, 3600))  # сначала немного спокойной жизни
    while True:
        name = rnd.choices(names, weights)[0]
        if OFFICE and name == "bad_deploy" and not work_hours(datetime.fromtimestamp(sim_now(), TZ)):
            continue  # релизы выкатывают только в рабочее время: выбираем другую проблему
        duration, changes, sev = plan(name)
        iid = f"inc-{int(sim_now())}"
        params = {k: v for k, v in changes.items()}
        write_event("incident", "start", id=iid, type=name, severity=sev,
                    duration_s=int(duration), params=params)
        with labels_lock:
            labels.update(incident=name, incident_severity=sev, incident_id=iid)
        stop_at = sim_now() + duration
        if name == "bot_attack":
            bot_attack(stop_at, changes["_bot_rate"])
        else:
            set_state(changes)
            while sim_now() < stop_at:
                sim_sleep(10)
            reset = dict(RESET)
            if name == "memory_leak":   # «перезапуск сервиса» освобождает память
                try:
                    reset["leak_epoch"] = int(get_state().get("leak_epoch", 0)) + 1
                except Exception:
                    reset["leak_epoch"] = int(time.time())
            set_state(reset)
        with labels_lock:
            labels.update(incident=None, incident_severity=None, incident_id=None)
        write_event("incident", "end", id=iid, type=name)
        sim_sleep(rnd.expovariate(1 / (CONFIG["incident_every_h"] * 3600)) + 600)


def nightly_job_loop():
    """office: каждую ночь около 02:00 бэкап и пересчёт отчётов грузят CPU на 35–55% на 20–40 мин.
    Это обычная жизнь сервера, а не проблема: метка incident остаётся пустой."""
    while True:
        now = datetime.fromtimestamp(sim_now(), TZ)
        start = now.replace(hour=2, minute=rnd.randint(0, 15), second=0, microsecond=0)
        if start <= now:
            start += timedelta(days=1)
        sim_sleep((start - now).total_seconds())
        with labels_lock:
            busy = labels["incident"] == "cpu_hog"
        if busy:
            continue
        pct, dur = rnd.randint(35, 55), rnd.uniform(1200, 2400)
        write_event("scheduled_job", "start", name="nightly_backup", cpu_pct=pct, duration_s=int(dur))
        try:
            set_state({"cpu_hog_pct": pct})
            sim_sleep(dur)
        finally:
            with labels_lock:
                busy = labels["incident"] == "cpu_hog"
            if not busy:
                set_state({"cpu_hog_pct": 0})
            write_event("scheduled_job", "end", name="nightly_backup")


_growth_start = None


def growth_factor():
    """Пользователей становится больше: (1 + рост) ^ (недель с первого запуска).
    Момент первого запуска хранится в файле, чтобы рост не сбрасывался после перезапуска."""
    global _growth_start
    g = CONFIG["growth_per_week"]
    if not g:
        return 1.0
    if _growth_start is None:
        path = os.path.join(CONFIG["data_dir"], "start_ts")
        try:
            _growth_start = float(open(path).read())
        except Exception:
            _growth_start = sim_now()
            try:
                with open(path, "w") as f:
                    f.write(str(_growth_start))
            except Exception:
                pass
    weeks = max(0.0, (sim_now() - _growth_start) / (7 * 86400))
    return (1 + g) ** weeks


# ---------------- главный цикл ----------------

def poisson(lam):
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(rnd.gauss(lam, math.sqrt(lam)))))
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rnd.random()
        if p <= l:
            return k
        k += 1


def main():
    os.makedirs(CONFIG["data_dir"], exist_ok=True)
    print("traffic config:", json.dumps({k: v for k, v in CONFIG.items() if k != "token"}), flush=True)
    threading.Thread(target=flash_loop, daemon=True).start()
    if OFFICE and CONFIG["token"]:
        threading.Thread(target=nightly_job_loop, daemon=True).start()
    if CONFIG["incidents"]:
        if not CONFIG["token"]:
            print("DEMO_TOKEN is empty: incidents disabled", flush=True)
        else:
            threading.Thread(target=incident_loop, daemon=True).start()
    noise = Noise()
    pool = ThreadPoolExecutor(max_workers=CONFIG["max_workers"])
    active = {"n": 0}
    active_lock = threading.Lock()

    def run(heavy):
        try:
            session(heavy)
        finally:
            with active_lock:
                active["n"] -= 1

    last_report = time.time()
    last_labels = 0
    while True:
        dt = datetime.fromtimestamp(sim_now(), TZ)
        hour = dt.hour + dt.minute / 60
        with labels_lock:
            flash = labels["flash_multiplier"]
        lam = (CONFIG["base_sessions"] * daily_curve(hour) * weekly_factor(dt) * noise.step() * flash
               * growth_factor())
        # шаг 1 секунда реального времени = SPEED секунд симуляции
        for _ in range(poisson(lam * min(SPEED, 1.0))):
            with active_lock:
                if active["n"] >= CONFIG["max_workers"]:
                    with stats_lock:
                        stats["dropped"] += 1
                    continue
                active["n"] += 1
            with stats_lock:
                stats["sessions"] += 1
            pool.submit(run, flash > 1.5 and rnd.random() < 0.7)
        if time.time() - last_labels >= 5:
            save_labels()
            last_labels = time.time()
        if time.time() - last_report >= 60:
            with stats_lock:
                snap = dict(stats)
                for k in stats:
                    stats[k] = 0
            print(json.dumps({"sim_time": dt.isoformat(timespec="minutes"), "lambda": round(lam, 2),
                              "active": active["n"], **snap}), flush=True)
            last_report = time.time()
        time.sleep(1)


if __name__ == "__main__":
    main()
