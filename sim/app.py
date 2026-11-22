"""ShopSim: учебный интернет-магазин с аукционом для Kezekshi.

Ведёт себя как настоящий сайт: у разных страниц разная цена (чтение из базы,
поиск по процессору, запись ставок с блокировками, оплата через медленный
внешний сервис), поэтому при наплыве людей сервер реально начинает тормозить
и сыпать ошибками, а при обычной нагрузке работает спокойно.

Сценарии проблем включает генератор трафика через /sim/state (с токеном).
Старые ручки /break/500, /break/cpu, /break/disk и /fix оставлены как были.

Каждый запрос пишется JSON-строкой в LOG_PATH: его читают CloudWatch Agent,
агент Kezekshi и collector.py (датасет для ML).

Запуск: gunicorn -c gunicorn.conf.py app:app
Первичная база: python app.py --init-db
"""
import gc
import hashlib
import hmac
import json
import multiprocessing
import os
import random
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, g, jsonify, request

LOG_PATH = os.environ.get("SIM_LOG", "/var/log/demoapp/app.log")
DATA_DIR = os.environ.get("SIM_DATA", "/var/lib/shopsim")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
DB_PATH = os.path.join(DATA_DIR, "shop.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
FILL_PATH = os.path.join(DATA_DIR, "disk-fill.bin")
DEMO_TOKEN = os.environ.get("DEMO_TOKEN", "")
DISK_LIMIT = 90.0          # выше этого загрузки отклоняются, чтобы не убить сервер
LEAK_CAP_MB = 150          # максимум «утечки» на один процесс (2 процесса = 300 МБ из 1 ГБ)
QUEUE_TIMEOUT = float(os.environ.get("SIM_QUEUE_TIMEOUT", "2.0"))  # сек ожидания в очереди до 503

DEFAULT_STATE = {
    "payment_latency": 0.15,     # средняя задержка платёжного сервиса, сек
    "checkout_error_rate": 0.0,  # доля ошибок оплаты («плохой релиз»)
    "search_error_rate": 0.0,    # доля ошибок поиска
    "leak_mb_per_min": 0.0,      # скорость утечки памяти на процесс
    "leak_epoch": 0,             # смена числа = «перезапуск», память освобождается
    "log_storm": 0,              # лишних DEBUG-строк на запрос
    "uploads_cleanup": True,     # работает ли очистка старых загрузок
    "cpu_hog_pct": 0,            # фоновая задача грузит CPU на N% (делает collector)
    "errors_until": 0,           # старая ручка /break/500
}

app = Flask(__name__)
_log_lock = threading.Lock()
_state_cache = {"mtime": None, "data": dict(DEFAULT_STATE)}
_inflight = {"n": 0}
_inflight_lock = threading.Lock()
_leak = {"chunks": [], "epoch": None, "acc": 0.0}
cpu_procs = []


# ---------------- общие функции ----------------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_log(row):
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode()
    fd = os.open(LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def log(level, msg, **extra):
    row = {"time": now_iso(), "level": level, "msg": msg}
    row.update(extra)
    write_log(row)


def load_state():
    try:
        mtime = os.stat(STATE_PATH).st_mtime
    except FileNotFoundError:
        return _state_cache["data"]
    if mtime != _state_cache["mtime"]:
        try:
            with open(STATE_PATH) as f:
                data = dict(DEFAULT_STATE)
                data.update(json.load(f))
            _state_cache.update(mtime=mtime, data=data)
        except (ValueError, OSError):
            pass
    return _state_cache["data"]


def save_state(changes):
    data = dict(load_state())
    data.update(changes)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_PATH)
    return data


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=0.3, isolation_level=None)
        g.db.row_factory = sqlite3.Row
    return g.db


def disk_percent():
    total, used, _ = shutil.disk_usage("/")
    return round(used * 100 / total, 1)


def require_token(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        given = request.headers.get("X-Demo-Token") or request.args.get("token") or ""
        if not DEMO_TOKEN or not hmac.compare_digest(given, DEMO_TOKEN):
            return jsonify(error="forbidden"), 403
        return view(*args, **kwargs)
    return wrapper


def broken_500():
    return time.time() < load_state().get("errors_until", 0)


# ---------------- утечка памяти (фон в каждом процессе) ----------------

def leak_loop():
    while True:
        time.sleep(5)
        st = load_state()
        if st.get("leak_epoch") != _leak["epoch"]:
            _leak["chunks"].clear()
            _leak["epoch"] = st.get("leak_epoch")
            gc.collect()
        rate = float(st.get("leak_mb_per_min") or 0)
        if rate > 0 and len(_leak["chunks"]) < LEAK_CAP_MB:
            _leak["acc"] += rate / 12          # раз в 5 секунд = 1/12 минуты
            while _leak["acc"] >= 1 and len(_leak["chunks"]) < LEAK_CAP_MB:
                _leak["chunks"].append(bytearray(b"x" * (1024 * 1024)))
                _leak["acc"] -= 1


threading.Thread(target=leak_loop, daemon=True).start()


# ---------------- журнал каждого запроса ----------------

@app.before_request
def before():
    g.t0 = time.perf_counter()
    with _inflight_lock:
        _inflight["n"] += 1
        g.inflight = _inflight["n"]
    # Как балансировщик нагрузки: если запрос слишком долго ждал свободного потока, отдаём 503
    sent = request.headers.get("X-Request-Start")
    g.queue_ms = 0.0
    if sent:
        try:
            g.queue_ms = max(0.0, (time.time() - float(sent)) * 1000)
        except ValueError:
            pass
    if g.queue_ms > QUEUE_TIMEOUT * 1000 and request.path != "/health":
        g.msg = "request timed out in queue, server overloaded"
        return jsonify(error="server overloaded"), 503


@app.after_request
def after(resp):
    with _inflight_lock:
        _inflight["n"] -= 1
    if request.path == "/health":
        return resp
    ms = round((time.perf_counter() - g.t0) * 1000, 1)
    status = resp.status_code
    level = "ERROR" if status >= 500 else "WARN" if status >= 400 else "INFO"
    row = {"time": now_iso(), "level": level, "msg": getattr(g, "msg", "request"),
           "method": request.method, "path": request.url_rule.rule if request.url_rule else request.path,
           "status": status, "latency_ms": round(ms + g.queue_ms, 1), "queue_ms": round(g.queue_ms, 1),
           "inflight": g.inflight, "pid": os.getpid()}
    write_log(row)
    storm = int(load_state().get("log_storm") or 0)
    for i in range(min(storm, 50)):
        log("DEBUG", "verbose debug trace", path=row["path"], step=i,
            payload="x" * 200)
    return resp


@app.teardown_request
def teardown(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def fail(status, msg):
    g.msg = msg
    return jsonify(error=msg), status


# ---------------- страницы магазина ----------------

@app.route("/health")
def health():
    return "ok"


@app.route("/")
def home():
    time.sleep(random.uniform(0.04, 0.09))  # ожидание базы/сети
    rows = db().execute("SELECT id, title, price FROM lots ORDER BY ends_at LIMIT 12").fetchall()
    return jsonify(featured=[dict(r) for r in rows])


@app.route("/catalog")
def catalog():
    time.sleep(random.uniform(0.06, 0.14))  # ожидание базы/сети
    page = max(0, min(int(request.args.get("page", 0)), 400))
    cat = request.args.get("category")
    q = "SELECT id, title, price FROM lots"
    args = []
    if cat:
        q += " WHERE category = ?"
        args.append(cat)
    q += " ORDER BY price DESC LIMIT 24 OFFSET ?"
    args.append(page * 24)
    rows = db().execute(q, args).fetchall()
    return jsonify(items=[dict(r) for r in rows])


@app.route("/item/<int:lot_id>")
def item(lot_id):
    time.sleep(random.uniform(0.04, 0.1))  # ожидание базы/сети
    lot = db().execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
    if not lot:
        return fail(404, "lot not found")
    bids = db().execute("SELECT amount, ts FROM bids WHERE lot_id = ? ORDER BY ts DESC LIMIT 20",
                        (lot_id,)).fetchall()
    return jsonify(lot=dict(lot), bids=[dict(b) for b in bids])


@app.route("/search")
def search():
    if broken_500():
        return fail(500, "database connection refused")
    if random.random() < float(load_state().get("search_error_rate") or 0):
        return fail(500, "search index unavailable")
    time.sleep(random.uniform(0.02, 0.05))  # ожидание базы/сети
    term = (request.args.get("q") or "lamp")[:30]
    rows = db().execute("SELECT id, title, price FROM lots WHERE title LIKE ? LIMIT 400",
                        (f"%{term}%",)).fetchall()
    # «ранжирование»: реальная работа процессора, растёт с числом совпадений
    scored = []
    for r in rows:
        h = hashlib.sha256(f"{term}:{r['title']}".encode()).digest()
        for _ in range(80):
            h = hashlib.sha256(h).digest()
        scored.append((h[0], dict(r)))
    scored.sort(key=lambda x: x[0], reverse=True)
    return jsonify(results=[s[1] for s in scored[:20]], total=len(rows))


@app.route("/bid", methods=["POST"])
def bid():
    body = request.get_json(silent=True) or {}
    lot_id = int(body.get("lot_id") or random.randint(1, 20000))
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("SELECT COALESCE(MAX(amount), 0) FROM bids WHERE lot_id = ?",
                           (lot_id,)).fetchone()[0]
        time.sleep(random.uniform(0.004, 0.02))  # проверка лимитов, антифрод
        amount = round(max(cur, 100) * random.uniform(1.01, 1.08), 2)
        conn.execute("INSERT INTO bids(lot_id, amount, ts, user_id) VALUES (?,?,?,?)",
                     (lot_id, amount, time.time(), body.get("user", "anon")))
        conn.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        return fail(500, f"bid failed: {exc}")
    return jsonify(ok=True, lot_id=lot_id, amount=amount)


@app.route("/checkout", methods=["POST"])
def checkout():
    st = load_state()
    if random.random() < float(st.get("checkout_error_rate") or 0):
        return fail(500, "checkout failed: NullPointerException in OrderService")
    delay = float(st.get("payment_latency") or 0.15) * random.lognormvariate(0, 0.35)
    if delay > 6:
        time.sleep(6)
        return fail(504, "payment provider timeout")
    time.sleep(delay)
    if delay > 3 and random.random() < 0.3:
        return fail(502, "payment provider error")
    return jsonify(ok=True, order=random.randint(10**6, 10**7))


@app.route("/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    pwd = str(body.get("password", ""))
    hashlib.pbkdf2_hmac("sha256", pwd.encode(), b"shopsim-salt", 20000)
    if pwd != "correct-horse":
        g.msg = "invalid credentials"
        return jsonify(error="invalid credentials"), 401
    return jsonify(ok=True)


@app.route("/upload", methods=["POST"])
def upload():
    if disk_percent() >= DISK_LIMIT:
        return fail(507, "no space left on device")
    size = random.randint(50, 400) * 1024
    name = f"{int(time.time() * 1000)}-{random.randint(0, 99999)}.jpg"
    with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
        f.write(os.urandom(size))
    return jsonify(ok=True, size=size)


@app.errorhandler(404)
def not_found(_):
    g.msg = "not found"
    return jsonify(error="not found"), 404


# ---------------- управление сценариями ----------------

@app.route("/sim/state", methods=["GET", "POST"])
@require_token
def sim_state():
    if request.method == "GET":
        return jsonify(load_state())
    changes = request.get_json(silent=True) or {}
    allowed = {k: v for k, v in changes.items() if k in DEFAULT_STATE}
    data = save_state(allowed)
    log("INFO", "sim state changed", changes=allowed)
    return jsonify(data)


# ---------------- старые ручки поломок (совместимость) ----------------

def burn(seconds):
    end = time.time() + seconds
    while time.time() < end:
        pass


def stop_cpu():
    for p in cpu_procs:
        if p.is_alive():
            p.terminate()
    cpu_procs.clear()


@app.route("/break/500", methods=["POST"])
@require_token
def break_500():
    save_state({"errors_until": time.time() + 120})
    log("WARN", "break started", kind="500", seconds=120)
    return jsonify(ok=True)


@app.route("/break/cpu", methods=["POST"])
@require_token
def break_cpu():
    stop_cpu()
    cores = os.cpu_count() or 1
    for _ in range(cores):
        p = multiprocessing.Process(target=burn, args=(300,), daemon=True)
        p.start()
        cpu_procs.append(p)
    log("WARN", "break started", kind="cpu", seconds=300, cores=cores)
    return jsonify(ok=True)


@app.route("/break/disk", methods=["POST"])
@require_token
def break_disk():
    if os.path.exists(FILL_PATH):
        os.remove(FILL_PATH)
    total, used, _ = shutil.disk_usage("/")
    size = int(total * 0.92) - used
    if size > 0:
        with open(FILL_PATH, "wb") as f:
            os.posix_fallocate(f.fileno(), 0, size)
    log("WARN", "break started", kind="disk", used_percent=disk_percent())
    return jsonify(ok=True)


@app.route("/fix", methods=["POST"])
@require_token
def fix():
    stop_cpu()
    if os.path.exists(FILL_PATH):
        os.remove(FILL_PATH)
    st = load_state()
    save_state({**DEFAULT_STATE, "leak_epoch": int(st.get("leak_epoch", 0)) + 1})
    log("INFO", "fixed, back to normal", used_percent=disk_percent())
    return jsonify(ok=True)


# ---------------- первичная база ----------------

WORDS = ["vintage", "lamp", "watch", "camera", "guitar", "chair", "sneakers", "phone",
         "laptop", "painting", "vinyl", "bicycle", "coin", "jacket", "ring", "console",
         "book", "speaker", "rug", "mirror", "knife", "headphones", "drone", "tablet"]
CATS = ["electronics", "fashion", "home", "art", "sport", "collectibles"]


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lots(id INTEGER PRIMARY KEY, title TEXT, category TEXT,
                                        price REAL, ends_at REAL);
        CREATE TABLE IF NOT EXISTS bids(id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id INTEGER,
                                        amount REAL, ts REAL, user_id TEXT);
        CREATE INDEX IF NOT EXISTS idx_lots_cat ON lots(category, price);
        CREATE INDEX IF NOT EXISTS idx_lots_price ON lots(price);
        CREATE INDEX IF NOT EXISTS idx_lots_ends ON lots(ends_at);
        CREATE INDEX IF NOT EXISTS idx_bids_lot ON bids(lot_id, ts);
    """)
    if conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] == 0:
        rnd = random.Random(42)
        now = time.time()
        rows = [(i, f"{rnd.choice(WORDS)} {rnd.choice(WORDS)} #{i}", rnd.choice(CATS),
                 round(rnd.uniform(5, 5000), 2), now + rnd.uniform(0, 7 * 86400))
                for i in range(1, 20001)]
        conn.executemany("INSERT INTO lots VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    if not os.path.exists(STATE_PATH):
        with open(STATE_PATH, "w") as f:
            json.dump(DEFAULT_STATE, f)
    print("database ready:", DB_PATH)


if __name__ == "__main__":
    if "--init-db" in sys.argv:
        init_db()
    else:
        app.run(host="0.0.0.0", port=8080, threaded=True)
