"""Объяснение инцидента без внешнего ИИ: правила и шаблоны поверх наших данных.

На вход: тип аларма, ряды метрик сервера за последние часы, сводка ERROR-строк логов.
На выход: заголовок, важность, вероятная причина, шаги, код причины.

Причина определяется так:
1. Если есть ML-классификатор (функция ml_cause ниже вернула ответ), берём его.
2. Иначе правила: сравниваем текущие значения с обычным уровнем за последние 3 часа,
   смотрим тренд и что пишут логи.
Все цифры в тексте берутся из метрик, поэтому текст не может «выдумать» факт.
"""
from statistics import median

RULES_VERSION = "rules-v1"

ALARM_TYPES = ("cpu-high", "mem-high", "disk-high", "app-errors", "server-down",
               "app-down", "slow-responses", "auth-attack", "log-flood")


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
    # (слова в логах, код, как объяснить). Порядок важен: первое совпадение побеждает.
    (("database is locked", "deadlock", "lock wait"), "db_lock",
     "база данных не успевает обрабатывать одновременные запросы (блокировки)"),
    (("payment", "gateway", "upstream", "provider"), "upstream_slow",
     "внешний сервис (например, платёжный) отвечает слишком медленно или с ошибками"),
    (("connection refused", "could not connect", "connection reset", "unavailable"), "dependency_down",
     "приложение не может подключиться к зависимому сервису (база данных, поиск, кэш или внешний API)"),
    (("timed out", "timeout", "overloaded"), "overload",
     "запросы не успевают обрабатываться и отваливаются по таймауту, сервер перегружен"),
    (("out of memory", "memoryerror", "oom", "cannot allocate"), "oom",
     "процессу не хватает памяти"),
    (("no space left", "disk full", "enospc"), "disk_full",
     "на диске закончилось место"),
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

def ml_cause(alarm, series, now_ts):
    """Модель определения причины. Пока не подключена: возвращает None,
    и работают правила ниже. Когда модель будет готова, она должна вернуть
    {"cause": код, "probability": 0..1, "explanation": текст}; ответ берётся,
    только если probability >= 0.6."""
    return None


# ---------------- правила по типам алармов ----------------

def pct(v):
    return "нет данных" if v is None else f"{v:.0f}%"


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

    def load_text():
        return f"запросов в {load_ratio:.1f} раза больше обычного" if load_ratio else ""

    if a == "cpu-high":
        r["title"] = f"Высокая нагрузка на CPU: {pct(value)}"
        r["severity"] = "high" if value >= 95 else "medium"
        if load_ratio and load_ratio >= 1.8:
            r["cause_code"] = "traffic_surge"
            r["root_cause"] = f"Наплыв посетителей: {load_text()}, процессор не справляется с потоком."
            r["recommendation_steps"] = [
                "Проверить, не рекламная ли это акция или атака (откуда идут запросы)",
                "Временно добавить мощности: тип инстанса крупнее или второй сервер",
                "Включить кэширование самых частых страниц",
            ]
        else:
            r["cause_code"] = "background_process"
            r["root_cause"] = ("Посетителей обычное количество, значит процессор загружает не сайт, "
                               "а фоновая задача: бэкап, пересчёт отчётов или зависший скрипт.")
            r["recommendation_steps"] = [
                "Посмотреть самые тяжёлые процессы: top -o %CPU",
                "Если это плановая задача, перенести её на ночь",
                "Если процесс завис, остановить его",
            ]
    elif a == "mem-high":
        trend = slope_per_hour(window(s.get("mem", []), 60, now_ts))
        r["title"] = f"Заканчивается память: {pct(value)}"
        r["severity"] = "critical" if value >= 97 else "high"
        if trend >= 3 and not (load_ratio and load_ratio >= 1.8):
            eta = (100 - (mem or value)) / trend * 60 if trend > 0 else None
            r["cause_code"] = "memory_leak"
            r["root_cause"] = (f"Память растёт на {trend:.1f}% в час при обычной нагрузке, это похоже на утечку "
                               f"памяти в приложении." + (f" При таком темпе закончится примерно через {eta:.0f} мин."
                                                         if eta else ""))
            r["recommendation_steps"] = [
                "Перезапустить сервис приложения, это временно освободит память",
                "Проверить, что менялось в последнем деплое",
                "Ограничить память сервиса (MemoryMax в systemd), чтобы утечка не уронила сервер",
            ]
            r["auto_fix_possible"] = True
        elif load_ratio and load_ratio >= 1.8:
            r["cause_code"] = "traffic_surge"
            r["root_cause"] = f"Память заняла обработка большого потока запросов: {load_text()}."
            r["recommendation_steps"] = [
                "Уменьшить число одновременных воркеров приложения",
                "Добавить памяти: тип инстанса крупнее или swap",
            ]
        else:
            r["cause_code"] = "memory_pressure"
            r["root_cause"] = "Памяти не хватает процессам на сервере, резкого роста при этом не было."
            r["recommendation_steps"] = [
                "Посмотреть, кто занимает память: ps aux --sort=-%mem | head",
                "Перезапустить самый тяжёлый сервис или добавить памяти",
            ]
    elif a == "disk-high":
        trend = slope_per_hour(window(s.get("disk", []), 120, now_ts))
        r["title"] = f"Заканчивается место на диске: {pct(value)}"
        r["severity"] = "critical" if value >= 95 else "high"
        logs_heavy = load_ratio and load_ratio >= 3
        eta = (100 - (disk or value)) / trend if trend > 0.05 else None
        when = f" При текущем темпе ({trend:.1f}% в час) диск заполнится примерно через {eta:.1f} ч." if eta else ""
        if logs_heavy:
            r["cause_code"] = "log_growth"
            r["root_cause"] = "Диск забивают логи: приложение пишет их намного больше обычного." + when
        elif trend > 0.05:
            r["cause_code"] = "files_accumulate"
            r["root_cause"] = ("Диск постепенно заполняется файлами: загрузки, бэкапы или временные файлы "
                               "не удаляются." + when)
        else:
            r["cause_code"] = "disk_jump"
            r["root_cause"] = "Место на диске резко закончилось: скорее всего, появился один большой файл."
        r["recommendation_steps"] = [
            "Найти, что занимает место: sudo du -xh / --max-depth=2 | sort -h | tail",
            "Почистить старые логи и проверить настройки logrotate",
            "Проверить, работает ли задача очистки временных файлов и загрузок",
        ]
        r["auto_fix_possible"] = True
    elif a == "app-errors":
        r["title"] = f"Приложение отвечает ошибками: {value:.0f} в минуту"
        r["severity"] = "critical" if value >= 100 else "high" if value >= 30 else "medium"
        where = f" Больше всего ошибок на {top_path}." if top_path and top_share >= 0.6 else ""
        if log_code:
            r["cause_code"] = log_code
            r["root_cause"] = f"Судя по логам, {log_expl}.{where}"
        elif top_path and top_share >= 0.6:
            r["cause_code"] = "endpoint_broken"
            r["root_cause"] = f"Ошибки почти только на {top_path}: сломалась одна часть приложения, остальное работает."
        else:
            r["cause_code"] = "bad_deploy"
            r["root_cause"] = ("Ошибки идут по разным адресам без явной внешней причины. "
                               "Так обычно выглядит неудачное обновление или сломанная настройка.")
        steps = {
            "db_lock": ["Проверить долгие запросы и блокировки в базе",
                        "Снизить число одновременных записей (очередь, повтор с задержкой)"],
            "overload": ["Проверить нагрузку: сколько запросов приходит сейчас",
                         "Добавить мощности или ограничить поток запросов"],
            "upstream_slow": ["Проверить статус внешнего сервиса (платёжного шлюза)",
                              "Уменьшить таймауты и показать пользователю понятную ошибку"],
            "dependency_down": ["Проверить, запущена ли база данных или зависимый сервис",
                                "Проверить сеть и доступы до него"],
        }.get(r["cause_code"], ["Посмотреть последние ошибки: journalctl -u <сервис> -n 50",
                                "Если недавно был деплой, откатить его"])
        r["recommendation_steps"] = steps + ["Перезапустить сервис, если ошибки не прекращаются"]
        r["auto_fix_possible"] = r["cause_code"] in ("bad_deploy", "endpoint_broken")
    elif a == "slow-responses":
        r["title"] = f"Сайт отвечает медленно: {value / 1000:.1f} с"
        r["severity"] = "high" if value >= 5000 else "medium"
        if cpu is not None and cpu >= 80:
            r["cause_code"] = "cpu_saturation"
            r["root_cause"] = f"Процессор загружен на {pct(cpu)}, запросам не хватает CPU."
        elif load_ratio and load_ratio >= 1.8:
            r["cause_code"] = "traffic_surge"
            r["root_cause"] = (f"Наплыв посетителей: {load_text()}. Запросы стоят в очереди, "
                               "сервер ещё отвечает, но всё медленнее. Если поток вырастет, начнутся ошибки.")
        elif log_code:
            r["cause_code"] = log_code
            r["root_cause"] = f"Нагрузка обычная, но по логам {log_expl}."
        else:
            r["cause_code"] = "slow_dependency"
            r["root_cause"] = ("Нагрузка и CPU в норме, значит тормозит что-то, от чего зависит сайт: "
                               "база данных или внешний сервис.")
        r["recommendation_steps"] = [
            "Проверить время ответа базы данных и внешних сервисов",
            "Посмотреть, какие страницы тормозят сильнее всего",
            "Если это наплыв, добавить мощности или включить кэш",
        ]
    elif a == "auth-attack":
        nf = last(s.get("not_found"), 0) or 0
        r["title"] = f"Похоже на атаку: {value:.0f} неудачных входов в минуту"
        r["severity"] = "high" if value >= 500 else "medium"
        r["cause_code"] = "brute_force"
        r["root_cause"] = ("Кто-то массово перебирает пароли."
                           + (f" Одновременно идёт сканирование: {nf:.0f} запросов в минуту к несуществующим "
                              "адресам (поиск админок и файлов настроек)." if nf >= 30 else ""))
        r["recommendation_steps"] = [
            "Найти IP-адреса атакующих в логах веб-сервера",
            "Заблокировать их (Security Group, WAF или fail2ban)",
            "Включить ограничение попыток входа и капчу",
        ]
    elif a == "log-flood":
        errors_now = last(s.get("errors"), 0) or 0
        r["title"] = f"Шквал логов: {value:.0f} строк в минуту"
        r["severity"] = "low"
        if errors_now >= 5:
            r["cause_code"] = "error_loop"
            r["root_cause"] = "Приложение раз за разом пишет одну и ту же ошибку, из-за этого логов очень много."
        else:
            r["cause_code"] = "verbose_logging"
            r["root_cause"] = ("Логов в разы больше обычного, а ошибок нет. Скорее всего, включён подробный "
                               "режим (DEBUG). Это само по себе не поломка, но так быстро заканчивается диск.")
        r["recommendation_steps"] = [
            "Проверить уровень логирования приложения и вернуть INFO",
            "Проверить, что logrotate настроен и диск не заполняется",
        ]
    elif a == "app-down":
        r["title"] = "Приложение не отвечает"
        r["severity"] = "critical"
        r["cause_code"] = "app_down"
        load_zero = load_now is not None and load_now < 1
        r["root_cause"] = ("Сервер работает, но приложение не отвечает на проверку. "
                           + ("Логи тоже перестали писаться, значит процесс приложения остановлен или упал."
                              if load_zero else "Логи ещё пишутся, значит процесс жив, но завис или перегружен."))
        r["recommendation_steps"] = [
            "Проверить статус: systemctl status <сервис>",
            "Посмотреть последние строки лога перед падением",
            "Перезапустить сервис",
        ]
        r["auto_fix_possible"] = True
    elif a == "server-down":
        r["title"] = "Сервер не выходит на связь"
        r["severity"] = "critical"
        r["cause_code"] = "server_down"
        r["root_cause"] = ("Агент мониторинга 3 минуты не присылает данные: сервер выключен или перезагружается, "
                           "пропала сеть, или остановлен сам агент.")
        r["recommendation_steps"] = [
            "Проверить состояние инстанса в консоли AWS (Instance state, Status checks)",
            "Если сервер работает, проверить агента: systemctl status kezekshi-agent",
            "Проверить сеть и Security Group",
        ]
    else:
        r["title"] = f"Сработал аларм {alarm_name}"
        r["severity"] = "medium"
        r["root_cause"] = f"Значение {value} превысило порог {threshold}."
        r["recommendation_steps"] = ["Проверить метрику в CloudWatch", "Посмотреть логи сервера"]

    ml = ml_cause(a, s, now_ts)
    if ml and ml.get("probability", 0) >= 0.6:
        r["cause_code"] = ml["cause"]
        r["root_cause"] = ml.get("explanation") or r["root_cause"]
        r["cause_source"] = "ml"
        r["cause_probability"] = ml["probability"]
    else:
        r["cause_source"] = RULES_VERSION
    r["load_ratio"] = round(load_ratio, 2) if load_ratio else None
    return r
