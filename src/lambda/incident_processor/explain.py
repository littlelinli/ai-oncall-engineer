"""Объяснение инцидента без внешнего ИИ: правила и шаблоны поверх наших данных.

На вход: тип аларма, ряды метрик сервера за последние часы, сводка ERROR-строк логов.
На выход: заголовок, важность, вероятная причина, шаги, код причины.
Все тексты на двух языках: поля title, root_cause, recommendation_steps по-русски
и те же поля с суффиксом _en по-английски.

Причина определяется так:
1. Если есть ML-классификатор (функция ml_cause ниже вернула ответ), берём его.
2. Иначе правила: сравниваем текущие значения с обычным уровнем за последние 3 часа,
   смотрим тренд и что пишут логи.
Все цифры в тексте берутся из метрик, поэтому текст не может «выдумать» факт.
"""
import json
import os
from statistics import median

RULES_VERSION = "rules-v1"

ALARM_TYPES = ("cpu-high", "mem-high", "disk-high", "app-errors", "server-down",
               "app-down", "slow-responses", "auth-attack", "log-flood")


def T(ru, en):
    """Текст на двух языках."""
    return {"ru": ru, "en": en}


def alarm_type(alarm_name):
    for t in ALARM_TYPES:
        if alarm_name.endswith(t):
            return t
    return "unknown"


# ---------------- работа с рядами ----------------

def last(points, default=None):
    return points[-1][1] if points else default


def window(points, minutes, now_ts):
    return [(t, v) for t, v in points if t >= now_ts - minutes * 60]


def baseline(points, now_ts, skip_min=30):
    """Обычный уровень: медиана за 3 часа, не считая последних skip_min минут."""
    old = [v for t, v in points if t < now_ts - skip_min * 60]
    return median(old) if len(old) >= 10 else None


def slope_per_hour(points):
    if len(points) < 5:
        return 0.0
    n = len(points)
    mx = sum(t for t, _ in points) / n
    my = sum(v for _, v in points) / n
    den = sum((t - mx) ** 2 for t, _ in points)
    return 0.0 if den == 0 else sum((t - mx) * (v - my) for t, v in points) / den * 3600


def ratio(now_value, base):
    if now_value is None or not base:
        return None
    return now_value / base


# ---------------- логи ----------------

KEYWORDS = (
    # (слова в логах, код, объяснение). Порядок важен: первое совпадение побеждает.
    (("database is locked", "deadlock", "lock wait"), "db_lock",
     T("база данных не успевает обрабатывать одновременные запросы (блокировки)",
       "the database cannot keep up with concurrent requests (lock contention)")),
    (("payment", "gateway", "upstream", "provider"), "upstream_slow",
     T("внешний сервис (например, платёжный) отвечает слишком медленно или с ошибками",
       "an external service (for example, the payment provider) is slow or returns errors")),
    (("connection refused", "could not connect", "connection reset", "unavailable"), "dependency_down",
     T("приложение не может подключиться к зависимому сервису (база данных, поиск, кэш или внешний API)",
       "the application cannot connect to a service it depends on (database, search, cache or external API)")),
    (("timed out", "timeout", "overloaded"), "overload",
     T("запросы не успевают обрабатываться и отваливаются по таймауту, сервер перегружен",
       "requests are not processed in time and fail with a timeout, the server is overloaded")),
    (("out of memory", "memoryerror", "oom", "cannot allocate"), "oom",
     T("процессу не хватает памяти", "the process runs out of memory")),
    (("no space left", "disk full", "enospc"), "disk_full",
     T("на диске закончилось место", "the disk is full")),
)
EXPLAIN = {code: text for _, code, text in KEYWORDS}


def line_code(msg):
    msg = (msg or "").lower()
    for words, code, _ in KEYWORDS:
        if any(w in msg for w in words):
            return code
    return None


def log_insight(log_summary):
    """Что говорят ERROR-строки: (код, объяснение, главный адрес или None, доля главного адреса).
    Код выбирается по числу строк: если больше всего ошибок без известных слов
    (например, исключение в коде), код None."""
    if not log_summary:
        return None, None, None, 0.0
    total = sum(x.get("count", 1) for x in log_summary) or 1
    by_code, by_path = {}, {}
    for x in log_summary:
        n = x.get("count", 1)
        c = line_code(x.get("msg"))
        by_code[c] = by_code.get(c, 0) + n
        if x.get("path"):
            by_path[x["path"]] = by_path.get(x["path"], 0) + n
    code = max(by_code, key=by_code.get)
    top_path, share = None, 0.0
    if by_path:
        top_path = max(by_path, key=by_path.get)
        share = by_path[top_path] / total
    return code, EXPLAIN.get(code), top_path, share


# ---------------- ML-классификатор (сюда встанет модель Алихана) ----------------

CAUSE_TEXT = {
    # код причины от классификатора -> объяснение для сообщения
    "payment_slow": T("Классификатор: медленно отвечает внешний сервис (оплата).",
                      "Classifier: an external service (payments) is slow."),
    "bad_deploy": T("Классификатор: похоже на неудачное обновление приложения.",
                    "Classifier: this looks like a bad application deploy."),
    "memory_leak": T("Классификатор: утечка памяти в приложении.", "Classifier: a memory leak in the application."),
    "log_storm": T("Классификатор: шквал логов, включён подробный режим.", "Classifier: a log flood, verbose logging is on."),
    "cleanup_broken": T("Классификатор: не работает очистка файлов, диск заполняется.",
                        "Classifier: file cleanup is broken, the disk is filling up."),
    "bot_attack": T("Классификатор: атака ботов (перебор паролей, сканирование).",
                    "Classifier: a bot attack (password guessing, scanning)."),
    "cpu_hog": T("Классификатор: процессор загружает фоновая задача.", "Classifier: a background job is using the CPU."),
    "app_down": T("Классификатор: приложение упало.", "Classifier: the application is down."),
    "traffic_surge": T("Классификатор: наплыв посетителей.", "Classifier: a traffic surge."),
}


def ml_cause(alarm, series, now_ts):
    """Причина от нейросети (Lambda kezekshi-ml, модель cause.onnx) или None:
    модели нет, функция не настроена, ошибка. Ответ берётся, если probability >= 0.6."""
    fn = os.environ.get("ML_FUNCTION")
    if not fn:
        return None
    try:
        import boto3  # здесь, чтобы explain.py можно было проверять без AWS
        payload = {"task": "cause", "series": {k: [[t, v] for t, v in pts[-60:]] for k, pts in series.items()}}
        resp = boto3.client("lambda").invoke(FunctionName=fn, Payload=json.dumps(payload).encode())
        out = json.loads(resp["Payload"].read())
    except Exception as e:
        print(f"ml cause: {e!r}")
        return None
    if not out.get("available") or str(out.get("version", "")).startswith("test-"):
        return None
    text = CAUSE_TEXT.get(out["cause"])
    if out["cause"] == "none" or not text:
        return None
    return {"cause": out["cause"], "probability": out.get("probability", 0), "explanation": text,
            "version": out.get("version")}


# ---------------- правила по типам алармов ----------------

def pct(v):
    return "n/a" if v is None else f"{v:.0f}%"


def explain(alarm_name, value, threshold, series, log_summary, now_ts):
    """series: {"cpu"|"mem"|"disk"|"errors"|"log_lines"|"auth"|"not_found"|"latency"|
               "health"|"response_ms": [(ts, value)] поминутно за ~3 часа}."""
    a = alarm_type(alarm_name)
    s = {k: v or [] for k, v in series.items()}
    cpu, mem, disk = last(s.get("cpu")), last(s.get("mem")), last(s.get("disk"))
    load_now = median([v for _, v in window(s.get("log_lines", []), 5, now_ts)] or [0]) or None
    load_ratio = ratio(load_now, baseline(s.get("log_lines", []), now_ts))
    log_code, log_expl, top_path, top_share = log_insight(log_summary)

    r = {"alarm_type": a, "cause_code": "unknown", "auto_fix_possible": False}
    lr = f"{load_ratio:.1f}" if load_ratio else ""

    if a == "cpu-high":
        r["title"] = T(f"Высокая нагрузка на CPU: {pct(value)}", f"High CPU load: {pct(value)}")
        r["severity"] = "high" if value >= 95 else "medium"
        if load_ratio and load_ratio >= 1.8:
            r["cause_code"] = "traffic_surge"
            r["root_cause"] = T(
                f"Наплыв посетителей: запросов в {lr} раза больше обычного, процессор не справляется с потоком.",
                f"Traffic surge: {lr}x more requests than usual, the CPU cannot keep up.")
            r["steps"] = [
                T("Проверить, не рекламная ли это акция или атака (откуда идут запросы)",
                  "Check whether this is a promotion or an attack (where the requests come from)"),
                T("Временно добавить мощности: тип инстанса крупнее или второй сервер",
                  "Add capacity for now: a larger instance type or a second server"),
                T("Включить кэширование самых частых страниц", "Enable caching for the most visited pages"),
            ]
        else:
            r["cause_code"] = "background_process"
            r["root_cause"] = T(
                "Посетителей обычное количество, значит процессор загружает не сайт, а фоновая задача: "
                "бэкап, пересчёт отчётов или зависший скрипт.",
                "Traffic is normal, so the CPU is used not by the website but by a background job: "
                "a backup, a report rebuild or a stuck script.")
            r["steps"] = [
                T("Посмотреть самые тяжёлые процессы: top -o %CPU", "Find the heaviest processes: top -o %CPU"),
                T("Если это плановая задача, перенести её на ночь", "If it is a scheduled job, move it to the night"),
                T("Если процесс завис, остановить его", "If the process is stuck, stop it"),
            ]
    elif a == "mem-high":
        trend = slope_per_hour(window(s.get("mem", []), 60, now_ts))
        r["title"] = T(f"Заканчивается память: {pct(value)}", f"Memory is running out: {pct(value)}")
        r["severity"] = "critical" if value >= 97 else "high"
        if trend >= 3 and not (load_ratio and load_ratio >= 1.8):
            eta = (100 - (mem or value)) / trend * 60 if trend > 0 else None
            r["cause_code"] = "memory_leak"
            r["root_cause"] = T(
                f"Память растёт на {trend:.1f}% в час при обычной нагрузке, это похоже на утечку памяти в приложении."
                + (f" При таком темпе закончится примерно через {eta:.0f} мин." if eta else ""),
                f"Memory grows by {trend:.1f}% per hour while traffic is normal, this looks like a memory leak "
                f"in the application." + (f" At this rate it runs out in about {eta:.0f} min." if eta else ""))
            r["steps"] = [
                T("Перезапустить сервис приложения, это временно освободит память",
                  "Restart the application service, this frees memory for now"),
                T("Проверить, что менялось в последнем деплое", "Check what changed in the last deploy"),
                T("Ограничить память сервиса (MemoryMax в systemd), чтобы утечка не уронила сервер",
                  "Limit the service memory (MemoryMax in systemd) so the leak cannot take down the server"),
            ]
            r["auto_fix_possible"] = True
        elif load_ratio and load_ratio >= 1.8:
            r["cause_code"] = "traffic_surge"
            r["root_cause"] = T(
                f"Память заняла обработка большого потока запросов: запросов в {lr} раза больше обычного.",
                f"Memory is used up by a large flow of requests: {lr}x more than usual.")
            r["steps"] = [
                T("Уменьшить число одновременных воркеров приложения",
                  "Reduce the number of concurrent application workers"),
                T("Добавить памяти: тип инстанса крупнее или swap", "Add memory: a larger instance type or swap"),
            ]
        else:
            r["cause_code"] = "memory_pressure"
            r["root_cause"] = T("Памяти не хватает процессам на сервере, резкого роста при этом не было.",
                                "Processes on the server do not have enough memory, there was no sudden growth.")
            r["steps"] = [
                T("Посмотреть, кто занимает память: ps aux --sort=-%mem | head",
                  "See what uses memory: ps aux --sort=-%mem | head"),
                T("Перезапустить самый тяжёлый сервис или добавить памяти",
                  "Restart the heaviest service or add memory"),
            ]
    elif a == "disk-high":
        trend = slope_per_hour(window(s.get("disk", []), 120, now_ts))
        r["title"] = T(f"Заканчивается место на диске: {pct(value)}", f"Disk is running out of space: {pct(value)}")
        r["severity"] = "critical" if value >= 95 else "high"
        logs_heavy = load_ratio and load_ratio >= 3
        eta = (100 - (disk or value)) / trend if trend > 0.05 else None
        when = T(f" При текущем темпе ({trend:.1f}% в час) диск заполнится примерно через {eta:.1f} ч.",
                 f" At the current rate ({trend:.1f}% per hour) it will be full in about {eta:.1f} h.") \
            if eta else T("", "")
        if logs_heavy:
            r["cause_code"] = "log_growth"
            r["root_cause"] = T("Диск забивают логи: приложение пишет их намного больше обычного." + when["ru"],
                                "Logs fill the disk: the application writes far more of them than usual." + when["en"])
        elif trend > 0.05:
            r["cause_code"] = "files_accumulate"
            r["root_cause"] = T(
                "Диск постепенно заполняется файлами: загрузки, бэкапы или временные файлы не удаляются." + when["ru"],
                "The disk slowly fills with files: uploads, backups or temporary files are not removed." + when["en"])
        else:
            r["cause_code"] = "disk_jump"
            r["root_cause"] = T("Место на диске резко закончилось: скорее всего, появился один большой файл.",
                                "Disk space ran out suddenly: most likely one large file appeared.")
        r["steps"] = [
            T("Найти, что занимает место: sudo du -xh / --max-depth=2 | sort -h | tail",
              "Find what takes the space: sudo du -xh / --max-depth=2 | sort -h | tail"),
            T("Почистить старые логи и проверить настройки logrotate", "Remove old logs and check logrotate settings"),
            T("Проверить, работает ли задача очистки временных файлов и загрузок",
              "Check that the cleanup job for temporary files and uploads is running"),
        ]
        r["auto_fix_possible"] = True
    elif a == "app-errors":
        r["title"] = T(f"Приложение отвечает ошибками: {value:.0f} в минуту",
                       f"The application returns errors: {value:.0f} per minute")
        r["severity"] = "critical" if value >= 100 else "high" if value >= 30 else "medium"
        where = T(f" Больше всего ошибок на {top_path}.", f" Most errors are on {top_path}.") \
            if top_path and top_share >= 0.6 else T("", "")
        if log_code:
            r["cause_code"] = log_code
            r["root_cause"] = T(f"Судя по логам, {log_expl['ru']}.{where['ru']}",
                                f"According to the logs, {log_expl['en']}.{where['en']}")
        elif top_path and top_share >= 0.6:
            r["cause_code"] = "endpoint_broken"
            r["root_cause"] = T(f"Ошибки почти только на {top_path}: сломалась одна часть приложения, остальное работает.",
                                f"Errors are almost only on {top_path}: one part of the application is broken, "
                                "the rest works.")
        else:
            r["cause_code"] = "bad_deploy"
            r["root_cause"] = T("Ошибки идут по разным адресам без явной внешней причины. "
                                "Так обычно выглядит неудачное обновление или сломанная настройка.",
                                "Errors come from different endpoints without a clear external cause. "
                                "This usually means a bad deploy or a broken setting.")
        steps = {
            "db_lock": [T("Проверить долгие запросы и блокировки в базе", "Check long queries and locks in the database"),
                        T("Снизить число одновременных записей (очередь, повтор с задержкой)",
                          "Reduce concurrent writes (a queue, retry with backoff)")],
            "overload": [T("Проверить нагрузку: сколько запросов приходит сейчас", "Check how many requests come in now"),
                         T("Добавить мощности или ограничить поток запросов", "Add capacity or rate limit requests")],
            "upstream_slow": [T("Проверить статус внешнего сервиса (платёжного шлюза)",
                                "Check the status of the external service (payment gateway)"),
                              T("Уменьшить таймауты и показать пользователю понятную ошибку",
                                "Lower the timeouts and show users a clear error message")],
            "dependency_down": [T("Проверить, запущена ли база данных или зависимый сервис",
                                  "Check that the database or the dependent service is running"),
                                T("Проверить сеть и доступы до него", "Check the network and access to it")],
        }.get(r["cause_code"], [T("Посмотреть последние ошибки: journalctl -u <сервис> -n 50",
                                  "Look at the latest errors: journalctl -u <service> -n 50"),
                                T("Если недавно был деплой, откатить его", "If there was a recent deploy, roll it back")])
        r["steps"] = steps + [T("Перезапустить сервис, если ошибки не прекращаются",
                                "Restart the service if the errors do not stop")]
        r["auto_fix_possible"] = r["cause_code"] in ("bad_deploy", "endpoint_broken")
    elif a == "slow-responses":
        r["title"] = T(f"Сайт отвечает медленно: {value / 1000:.1f} с", f"The website is slow: {value / 1000:.1f} s")
        r["severity"] = "high" if value >= 5000 else "medium"
        if cpu is not None and cpu >= 80:
            r["cause_code"] = "cpu_saturation"
            r["root_cause"] = T(f"Процессор загружен на {pct(cpu)}, запросам не хватает CPU.",
                                f"The CPU is at {pct(cpu)}, requests do not get enough CPU time.")
        elif load_ratio and load_ratio >= 1.8:
            r["cause_code"] = "traffic_surge"
            r["root_cause"] = T(f"Наплыв посетителей: запросов в {lr} раза больше обычного. Запросы стоят в очереди, "
                                "сервер ещё отвечает, но всё медленнее. Если поток вырастет, начнутся ошибки.",
                                f"Traffic surge: {lr}x more requests than usual. Requests wait in a queue, the server "
                                "still answers but slower and slower. If traffic grows further, errors will start.")
        elif log_code:
            r["cause_code"] = log_code
            r["root_cause"] = T(f"Нагрузка обычная, но по логам {log_expl['ru']}.",
                                f"Traffic is normal, but according to the logs {log_expl['en']}.")
        else:
            r["cause_code"] = "slow_dependency"
            r["root_cause"] = T("Нагрузка и CPU в норме, значит тормозит что-то, от чего зависит сайт: "
                                "база данных или внешний сервис.",
                                "Traffic and CPU are normal, so something the website depends on is slow: "
                                "the database or an external service.")
        r["steps"] = [
            T("Проверить время ответа базы данных и внешних сервисов",
              "Check the response time of the database and external services"),
            T("Посмотреть, какие страницы тормозят сильнее всего", "Find which pages are the slowest"),
            T("Если это наплыв, добавить мощности или включить кэш", "If it is a traffic surge, add capacity or enable caching"),
        ]
    elif a == "auth-attack":
        nf = last(s.get("not_found"), 0) or 0
        scan = nf >= 30
        r["title"] = T(f"Похоже на атаку: {value:.0f} неудачных входов в минуту",
                       f"Possible attack: {value:.0f} failed logins per minute")
        r["severity"] = "high" if value >= 500 else "medium"
        r["cause_code"] = "brute_force"
        r["root_cause"] = T(
            "Кто-то массово перебирает пароли."
            + (f" Одновременно идёт сканирование: {nf:.0f} запросов в минуту к несуществующим адресам "
               "(поиск админок и файлов настроек)." if scan else ""),
            "Someone is brute-forcing passwords."
            + (f" There is also a scan: {nf:.0f} requests per minute to missing pages "
               "(looking for admin panels and config files)." if scan else ""))
        r["steps"] = [
            T("Найти IP-адреса атакующих в логах веб-сервера", "Find the attackers' IP addresses in the web server logs"),
            T("Заблокировать их (Security Group, WAF или fail2ban)", "Block them (Security Group, WAF or fail2ban)"),
            T("Включить ограничение попыток входа и капчу", "Enable login rate limiting and a captcha"),
        ]
    elif a == "log-flood":
        errors_now = last(s.get("errors"), 0) or 0
        r["title"] = T(f"Шквал логов: {value:.0f} строк в минуту", f"Log flood: {value:.0f} lines per minute")
        r["severity"] = "low"
        if errors_now >= 5:
            r["cause_code"] = "error_loop"
            r["root_cause"] = T("Приложение раз за разом пишет одну и ту же ошибку, из-за этого логов очень много.",
                                "The application writes the same error again and again, so there are a lot of logs.")
        else:
            r["cause_code"] = "verbose_logging"
            r["root_cause"] = T("Логов в разы больше обычного, а ошибок нет. Скорее всего, включён подробный "
                                "режим (DEBUG). Это само по себе не поломка, но так быстро заканчивается диск.",
                                "There are many times more logs than usual but no errors. Most likely verbose "
                                "(DEBUG) logging is on. It is not a failure by itself, but it fills the disk fast.")
        r["steps"] = [
            T("Проверить уровень логирования приложения и вернуть INFO", "Check the log level and set it back to INFO"),
            T("Проверить, что logrotate настроен и диск не заполняется",
              "Check that logrotate is set up and the disk is not filling up"),
        ]
    elif a == "app-down":
        load_zero = load_now is not None and load_now < 1
        r["title"] = T("Приложение не отвечает", "The application does not respond")
        r["severity"] = "critical"
        r["cause_code"] = "app_down"
        r["root_cause"] = T(
            "Сервер работает, но приложение не отвечает на проверку. "
            + ("Логи тоже перестали писаться, значит процесс приложения остановлен или упал." if load_zero
               else "Логи ещё пишутся, значит процесс жив, но завис или перегружен."),
            "The server is up, but the application does not answer the health check. "
            + ("Logs have stopped too, so the application process has stopped or crashed." if load_zero
               else "Logs are still written, so the process is alive but hung or overloaded."))
        r["steps"] = [
            T("Проверить статус: systemctl status <сервис>", "Check the status: systemctl status <service>"),
            T("Посмотреть последние строки лога перед падением", "Read the last log lines before the failure"),
            T("Перезапустить сервис", "Restart the service"),
        ]
        r["auto_fix_possible"] = True
    elif a == "server-down":
        r["title"] = T("Сервер не выходит на связь", "The server is not reachable")
        r["severity"] = "critical"
        r["cause_code"] = "server_down"
        r["root_cause"] = T("Агент мониторинга 3 минуты не присылает данные: сервер выключен или перезагружается, "
                            "пропала сеть, или остановлен сам агент.",
                            "The monitoring agent has sent no data for 3 minutes: the server is off or rebooting, "
                            "the network is down, or the agent itself has stopped.")
        r["steps"] = [
            T("Проверить состояние инстанса в консоли AWS (Instance state, Status checks)",
              "Check the instance in the AWS console (Instance state, Status checks)"),
            T("Если сервер работает, проверить агента: systemctl status kezekshi-agent",
              "If the server is up, check the agent: systemctl status kezekshi-agent"),
            T("Проверить сеть и Security Group", "Check the network and the Security Group"),
        ]
    else:
        r["title"] = T(f"Сработал аларм {alarm_name}", f"Alarm {alarm_name} fired")
        r["severity"] = "medium"
        r["root_cause"] = T(f"Значение {value} превысило порог {threshold}.",
                            f"The value {value} crossed the threshold {threshold}.")
        r["steps"] = [T("Проверить метрику в CloudWatch", "Check the metric in CloudWatch"),
                      T("Посмотреть логи сервера", "Read the server logs")]

    ml = ml_cause(a, s, now_ts)
    if ml and ml.get("probability", 0) >= 0.6:
        r["cause_code"] = ml["cause"]
        if isinstance(ml.get("explanation"), dict):
            r["root_cause"] = ml["explanation"]
        r["cause_source"] = "ml"
        r["cause_probability"] = ml["probability"]
        r["cause_model"] = ml.get("version")
    else:
        r["cause_source"] = RULES_VERSION

    # плоские поля: русский как основной (его читает сайт), английский с суффиксом _en
    out = {k: v for k, v in r.items() if k not in ("title", "root_cause", "steps")}
    out["title"], out["title_en"] = r["title"]["ru"], r["title"]["en"]
    out["root_cause"], out["root_cause_en"] = r["root_cause"]["ru"], r["root_cause"]["en"]
    out["recommendation_steps"] = [x["ru"] for x in r["steps"]]
    out["recommendation_steps_en"] = [x["en"] for x in r["steps"]]
    out["load_ratio"] = round(load_ratio, 2) if load_ratio else None
    return out
