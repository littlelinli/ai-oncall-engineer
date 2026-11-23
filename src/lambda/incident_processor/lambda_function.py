"""incident-analyzer: аларм -> логи и метрики -> объяснение по правилам (explain.py) ->
DynamoDB, Telegram, SNS. Внешний ИИ не используется.

Повторы: если по тому же серверу уже есть открытый инцидент за последние GROUP_MINUTES
минут, новый аларм добавляется к нему (related_alarms), отдельное сообщение не уходит.
Исключение: новый аларм серьёзнее (например, после CPU сервер пропал), тогда уходит
короткое сообщение об ухудшении.
"""
import json
import os
import time
import hashlib
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
 
import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

import explain
import recipients

LOG_GROUP = "/demoapp/app"
SERVICE = "demoapp"
# Подключённые серверы клиентов (агент Kezekshi -> /ingest)
SERVERS_LOG_GROUP = os.environ.get("SERVERS_LOG_GROUP", "/kezekshi/servers")
SERVERS_NAMESPACE = os.environ.get("METRICS_NAMESPACE", "Kezekshi")
PREDICTIONS_TABLE = os.environ.get("PREDICTIONS_TABLE", "kezekshi-predictions")
DEMO_SERVER_ID = os.environ.get("DEMO_SERVER_ID", "")  # сервер агента на демо-EC2: на нём можно автопочинку
GROUP_MINUTES = int(os.environ.get("GROUP_MINUTES", "15"))  # окно объединения алармов в один инцидент
# языки сообщений по порядку: ru, en или оба через запятую
LANGS = [x.strip() for x in os.environ.get("MESSAGE_LANGS", "ru,en").split(",") if x.strip() in ("ru", "en")] \
    or ["ru"]
ALLOWED_SEVERITY = {"low", "medium", "high", "critical"}
 
logs_client = boto3.client("logs")
cw_client = boto3.client("cloudwatch")
ssm_client = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")
 
 
# ---------- Логи ----------
 
def get_logs(alarm_time, minutes=10, max_lines=50, server_id=None):
    """Берёт строки логов за N минут до тревоги: демо-приложения или сервера клиента."""
    end = datetime.fromisoformat(alarm_time.replace("Z", "+00:00"))
    start = end - timedelta(minutes=minutes)
    where = {"logGroupName": LOG_GROUP}
    if server_id:
        where = {"logGroupName": SERVERS_LOG_GROUP, "logStreamNames": [server_id]}
    try:
        resp = logs_client.filter_log_events(
            **where,
            startTime=int(start.timestamp() * 1000),
            endTime=int(end.timestamp() * 1000),
            limit=200,
        )
        lines = [e["message"].strip() for e in resp.get("events", [])]
        return lines[-max_lines:]
    except Exception as e:
        return [f"[не удалось прочитать логи: {e}]"]
 
 
def summarize_logs(lines):
    """Сжимает повторяющиеся строки: сообщение + сколько раз встретилось."""
    counter = Counter()
    for line in lines:
        try:
            rec = json.loads(line)
            key = (rec.get("level"), rec.get("msg"), rec.get("path"), rec.get("status"))
        except Exception:
            key = ("RAW", line[:200], None, None)
        counter[key] += 1
 
    return [
        {"level": level, "msg": msg, "path": path, "status": status, "count": count}
        for (level, msg, path, status), count in counter.items()
    ]
 
 
# ---------- Статистика: CPU, диск, ошибки ----------
 
def metric_value(namespace, name, dimensions, end, minutes=20, stat="Average", pick="last"):
    """Значение метрики за N минут (последнее или пиковое) или None, если данных нет."""
    try:
        resp = cw_client.get_metric_statistics(
            Namespace=namespace,
            MetricName=name,
            Dimensions=dimensions,
            StartTime=end - timedelta(minutes=minutes),
            EndTime=end,
            Period=60,
            Statistics=[stat],
        )
        points = sorted(resp["Datapoints"], key=lambda p: p["Timestamp"])
        if not points:
            return None
        if pick == "max":
            return round(max(p[stat] for p in points), 1)
        return round(points[-1][stat], 1)
    except Exception as e:
        print(f"метрика {name} не прочиталась: {e}")
        return None
 
 
def get_server_stats(alarm_time, server_id):
    """То же для сервера клиента: метрики агента в namespace Kezekshi."""
    end = datetime.fromisoformat(alarm_time.replace("Z", "+00:00"))
    dims = [{"Name": "ServerId", "Value": server_id}]
    errors = metric_value(SERVERS_NAMESPACE, "ErrorCount", dims, end, minutes=10, stat="Sum", pick="max")
    return {
        "errors_per_min": errors if errors is not None else 0,
        "cpu": metric_value(SERVERS_NAMESPACE, "CpuPercent", dims, end),
        "disk": metric_value(SERVERS_NAMESPACE, "DiskPercent", dims, end),
    }


# наше имя -> (метрика агента, статистика)
SERIES = {
    "cpu": ("CpuPercent", "Average"),
    "mem": ("MemPercent", "Average"),
    "disk": ("DiskPercent", "Maximum"),
    "errors": ("ErrorCount", "Sum"),
    "log_lines": ("LogLines", "Sum"),
    "auth": ("AuthFailures", "Sum"),
    "not_found": ("NotFound", "Sum"),
    "latency": ("LatencyP95", "Average"),
    "health": ("HealthOk", "Average"),
    "response_ms": ("ResponseMs", "Average"),
}


def get_server_series(alarm_time, server_id, hours=3):
    """Поминутные ряды метрик агента за последние часы: {имя: [(ts, значение)]}."""
    end = datetime.fromisoformat(alarm_time.replace("Z", "+00:00"))
    dims = [{"Name": "ServerId", "Value": server_id}]
    out = {}
    for key, (name, stat) in SERIES.items():
        try:
            resp = cw_client.get_metric_statistics(
                Namespace=SERVERS_NAMESPACE, MetricName=name, Dimensions=dims,
                StartTime=end - timedelta(hours=hours), EndTime=end,
                Period=60, Statistics=[stat])
            out[key] = sorted((p["Timestamp"].timestamp(), p[stat]) for p in resp["Datapoints"])
        except Exception as e:
            print(f"ряд {name} не прочитался: {e}")
            out[key] = []
    return out


def stats_from_series(series):
    def last(k, nd=1):
        pts = series.get(k) or []
        return round(pts[-1][1], nd) if pts else None
    errors = [v for _, v in (series.get("errors") or [])[-10:]]
    return {
        "errors_per_min": max(errors) if errors else 0,
        "cpu": last("cpu"), "mem": last("mem"), "disk": last("disk"),
        "latency_ms": last("latency", 0), "auth_per_min": last("auth", 0),
    }


def server_name(description, server_id):
    """Описание аларма из /ingest заканчивается на «on <имя сервера>»."""
    if description and " on " in description:
        return description.rsplit(" on ", 1)[1]
    return server_id


def get_stats(alarm_time, instance_id):
    """errors_per_min, cpu, disk. Чего нет, там None (ошибки: 0)."""
    end = datetime.fromisoformat(alarm_time.replace("Z", "+00:00"))
 
    errors = metric_value("DemoApp", "AppErrors", [], end, minutes=10, stat="Sum", pick="max")
    stats = {"errors_per_min": errors if errors is not None else 0, "cpu": None, "disk": None}
    if not instance_id or instance_id == "unknown":
        return stats
 
    inst_dim = [{"Name": "InstanceId", "Value": instance_id}]
    wanted = (
        ("cpu", "cpu_usage_active", {"Name": "cpu", "Value": "cpu-total"}),
        ("disk", "disk_used_percent", {"Name": "path", "Value": "/"}),
    )
    for key, name, prefer in wanted:
        try:
            found = cw_client.list_metrics(
                Namespace="CWAgent", MetricName=name, Dimensions=inst_dim
            ).get("Metrics", [])
            if not found:
                continue
            best = [m for m in found if prefer in m["Dimensions"]]
            chosen = (best or found)[0]
            stats[key] = metric_value("CWAgent", name, chosen["Dimensions"], end)
        except Exception as e:
            print(f"{key} не прочитался: {e}")
    return stats
 
 
# ---------- Объяснение ----------
 
def sanitize(ai):
    """Сайт всегда получает значения из допустимого списка."""
    if not isinstance(ai, dict):
        ai = {}
 
    def fix(s):
        return str(s).replace("demo-app", SERVICE)
 
    sev = str(ai.get("severity", "")).lower()
    steps = ai.get("recommendation_steps")
    if not isinstance(steps, list) or not steps:
        steps = [f"Проверить статус сервиса {SERVICE}", "Посмотреть логи приложения"]
    return {
        "title": fix(str(ai.get("title") or "Инцидент")[:120]),
        "severity": sev if sev in ALLOWED_SEVERITY else "medium",
        "root_cause": fix(ai.get("root_cause") or "Причина не определена"),
        "recommendation_steps": [fix(s) for s in steps][:4],
        "auto_fix_possible": bool(ai.get("auto_fix_possible", False)),
    }
 
 
# ---------- DynamoDB ----------
 
def save_incident(incident):
    """True, если запись новая. False, если дубль. Без TABLE_NAME запись пропускается."""
    table_name = os.environ.get("TABLE_NAME")
    if not table_name:
        print("TABLE_NAME не задана, запись в DynamoDB пропущена")
        return True
    item = json.loads(json.dumps(incident), parse_float=Decimal)  # DynamoDB не принимает float
    item["incident_id"] = item["id"]
    try:
        dynamodb.Table(table_name).put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(incident_id)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
 
 
def save_auto_fix(inc_id, auto_fix):
    """Дописывает результат автопочинки в уже сохранённый инцидент."""
    table_name = os.environ.get("TABLE_NAME")
    if not table_name:
        return
    status = auto_fix.get("status", "none") if auto_fix.get("attempted") else "not_attempted"
    dynamodb.Table(table_name).update_item(
        Key={"incident_id": inc_id},
        UpdateExpression="SET auto_fix = :a, auto_fix_status = :s",
        ExpressionAttributeValues={":a": auto_fix, ":s": status},
    )
 
 
def resolve_incidents(alarm_name, ok_time):
    """При OK помечает все открытые инциденты этой тревоги как решённые."""
    table_name = os.environ.get("TABLE_NAME")
    if not table_name:
        return {"skipped": "TABLE_NAME не задана"}
    table = dynamodb.Table(table_name)
    flt = Attr("alarm_name").eq(alarm_name) & Attr("status").eq("open")
 
    items, kwargs = [], {"FilterExpression": flt}
    while True:
        resp = table.scan(**kwargs)
        items += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
 
    for it in items:
        fix = it.get("auto_fix") or {}
        by = "auto" if fix.get("status") == "Success" else "alarm"
        table.update_item(
            Key={"incident_id": it["incident_id"]},
            UpdateExpression="SET #s = :r, resolved_at = :t, resolved_by = :b",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":r": "resolved", ":t": ok_time, ":b": by},
        )
        if it.get("title"):  # старые записи без текста не трогаем
            notify_resolved(it, ok_time)
    return {"resolved": len(items), "alarm": alarm_name}
 
 
def scan_incidents(flt):
    table_name = os.environ.get("TABLE_NAME")
    if not table_name:
        return []
    table = dynamodb.Table(table_name)
    items, kwargs = [], {"FilterExpression": flt}
    while True:
        resp = table.scan(**kwargs)
        items += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


def iso_minus(alarm_time, minutes):
    t = datetime.fromisoformat(alarm_time.replace("Z", "+00:00")) - timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def open_incident_for(server_key, alarm_time):
    """Открытый инцидент этого сервера за последние GROUP_MINUTES минут или None."""
    try:
        items = scan_incidents(Attr("server_key").eq(server_key) & Attr("status").eq("open")
                               & Attr("timestamp").gte(iso_minus(alarm_time, GROUP_MINUTES)))
    except Exception as e:
        print(f"поиск открытого инцидента не удался: {e}")
        return None
    return max(items, key=lambda i: i["timestamp"]) if items else None


def similar_history(server_key, alarm_type, alarm_time):
    """Сколько раз за 7 дней на этом сервере был такой же тип проблемы."""
    try:
        items = scan_incidents(Attr("server_key").eq(server_key) & Attr("alarm_type").eq(alarm_type)
                               & Attr("timestamp").gte(iso_minus(alarm_time, 7 * 24 * 60))
                               & Attr("timestamp").lt(alarm_time))
    except Exception as e:
        print(f"история не прочиталась: {e}")
        return None
    if not items:
        return None
    lastinc = max(items, key=lambda i: i["timestamp"])
    return {"count": len(items), "last_time": lastinc["timestamp"],
            "last_cause": lastinc.get("root_cause", "")}


# какой риск ML-агента предупреждает о каком аларме
FORECAST_RISK = {"cpu-high": "cpu_overload", "mem-high": "memory_leak",
                 "disk-high": "disk_full", "app-errors": "errors_rising"}


def forecast_warning(server_id, alarm_type, alarm_time, max_hours=1):
    """Предупреждал ли ML-агент об этой проблеме заранее: {"minutes_before", "text"} или None."""
    code = FORECAST_RISK.get(alarm_type)
    if not code or not server_id:
        return None
    try:
        item = dynamodb.Table(PREDICTIONS_TABLE).get_item(
            Key={"server_id": server_id, "ts": f"warn#{code}"}).get("Item")
    except Exception as e:
        print(f"прогноз не прочитался: {e}")
        return None
    if not item:
        return None
    alarm_ts = datetime.fromisoformat(alarm_time.replace("Z", "+00:00")).timestamp()
    ahead = (alarm_ts - float(item["sent_at"])) / 60
    if 0 < ahead <= max_hours * 60:
        return {"minutes_before": int(ahead), "text": item.get("text", "")}
    return None


SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


# ---------- Автопочинка (SSM Run Command) ----------
 
def fix_plan(alarm_name):
    """Фиксированная команда починки: запрос /fix к приложению с токеном."""
    cmd = (
        "T=$(cut -d= -f2 /etc/demoapp.env); "
        "curl -sf -X POST -H \"X-Demo-Token: $T\" localhost:8080/fix"
    )
    return cmd, "снятие поломки через /fix"
 
 
def run_auto_fix(inc):
    """Запускает починку на сервере и коротко ждёт результат. Возвращает словарь для JSON."""
    if os.environ.get("AUTO_FIX", "true").lower() != "true":
        return {"attempted": False, "reason": "автопочинка выключена"}
    instance_id = inc.get("instance_id", "")
    if not instance_id.startswith("i-"):
        return {"attempted": False, "reason": "неизвестен id сервера"}
 
    command, action = fix_plan(inc["alarm_name"])
    result = {"attempted": True, "action": action, "command": command, "status": "Pending"}
    try:
        resp = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
            Comment=f"auto-fix {inc['id']}"[:100],
        )
        command_id = resp["Command"]["CommandId"]
        result["command_id"] = command_id
 
        for _ in range(6):  # ждём до ~18 секунд
            time.sleep(3)
            try:
                inv = ssm_client.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id
                )
            except ClientError:
                continue  # команда ещё не зарегистрировалась
            result["status"] = inv["Status"]
            if inv["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
                break
    except Exception as e:
        result["status"] = "Error"
        result["error"] = str(e)[:300]
    return result
 
 
# ---------- Уведомления ----------
 
def fmt(v):
    """Красиво показывает процент или «нет данных»."""
    return "нет данных" if v is None else f"{v}%"
 
 
L = {
    "ru": {"server": "Сервер", "cause": "Вероятная причина", "todo": "Что сделать", "mem": "Память",
           "disk": "Диск", "errors": "Ошибок/мин", "latency": "Задержка p95", "nodata": "нет данных", "sec": "с",
           "forecast": "🔮 ML-агент предупреждал об этом за {m} мин.",
           "history": "📎 За неделю такое уже было: {n}, последний раз {t} UTC.",
           "worse": "Ухудшение на {server}", "cont": "Это продолжение инцидента «{title}»."},
    "en": {"server": "Server", "cause": "Likely cause", "todo": "What to do", "mem": "Memory",
           "disk": "Disk", "errors": "Errors/min", "latency": "Latency p95", "nodata": "n/a", "sec": "s",
           "forecast": "🔮 The ML agent warned about this {m} min in advance.",
           "history": "📎 This happened {n} time(s) in the last week, last time {t} UTC.",
           "worse": "Getting worse on {server}", "cont": "This continues the incident \"{title}\"."},
}


def tr(inc, field, lang):
    """Поле инцидента на нужном языке (английское с суффиксом _en, если есть)."""
    if lang == "en" and inc.get(f"{field}_en"):
        return inc[f"{field}_en"]
    return inc[field]


def stats_line(st, lang="ru"):
    t = L[lang]
    f = (lambda v: t["nodata"] if v is None else f"{v}%")
    errors = st.get("errors_per_min")
    parts = [f"CPU: {f(st.get('cpu'))}"]
    if "mem" in st:
        parts.append(f"{t['mem']}: {f(st.get('mem'))}")
    parts += [f"{t['disk']}: {f(st.get('disk'))}",
              f"{t['errors']}: {t['nodata'] if errors is None else int(errors)}"]
    if st.get("latency_ms") is not None:
        parts.append(f"{t['latency']}: {st['latency_ms'] / 1000:.1f} {t['sec']}")
    return "  ".join(parts)


def extra_lines(inc, lang="ru"):
    t, lines = L[lang], []
    fw = inc.get("forecast_warning")
    if fw:
        lines.append(t["forecast"].format(m=fw["minutes_before"]))
    hist = inc.get("history")
    if hist:
        lines.append(t["history"].format(n=hist["count"], t=hist["last_time"][:16].replace("T", " ")))
    return lines


def message_text(inc, lang="ru", icons=True):
    t = L[lang]
    icon = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}.get(inc["severity"], "⚪")
    steps = "\n".join(f"{i}) {x}" for i, x in enumerate(tr(inc, "recommendation_steps", lang), 1))
    parts = [
        f"{icon + ' ' if icons else ''}{tr(inc, 'title', lang)}",
        f"{t['server']}: {inc['server']}",
        stats_line(inc["stats"], lang),
        "",
        f"{t['cause']}: {tr(inc, 'root_cause', lang)}",
        "",
        f"{t['todo']}:\n{steps}",
    ]
    extra = extra_lines(inc, lang)
    if extra:
        parts += [""] + extra
    return "\n".join(parts)


def all_languages(inc, icons=True, langs=None):
    """Сообщение на языках langs (по умолчанию MESSAGE_LANGS), блоки через разделитель."""
    return "\n\n— — —\n\n".join(message_text(inc, lang, icons) for lang in (langs or LANGS))


def telegram(text, chat_id):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or not chat_id:
        print("Telegram не настроен, пропускаю")
        return
    data = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10).read()


def send_telegram(inc):
    """Владельцу сервера на его языке и/или в чат команды (см. recipients.py)."""
    for chat_id, langs in recipients.telegram_targets(inc.get("server_id"), LANGS):
        try:
            telegram(all_languages(inc, langs=langs), chat_id)
        except Exception as e:
            print(f"telegram {chat_id}: {e}")


def send_sns(inc):
    """Почта. Сервер с владельцем: письмо владельцу, если он включил почту на сайте
    (тема SNS USER_ALERTS_TOPIC_ARN, подписка с фильтром по owner). Без владельца: почта команды."""
    try:
        owner, user = recipients.owner_profile(inc.get("server_id"))
    except Exception as e:
        print(f"owner lookup: {e!r}")
        owner, user = None, {}
    subject_lang = "en" if recipients.langs_of(user, LANGS) == ["en"] else "ru"
    subject = f"[{inc['severity'].upper()}] {tr(inc, 'title', subject_lang)}"[:100]
    if owner:
        topic = os.environ.get("USER_ALERTS_TOPIC_ARN")
        if topic and user.get("email_alerts"):
            boto3.client("sns").publish(
                TopicArn=topic, Subject=subject,
                Message=all_languages(inc, icons=False, langs=recipients.langs_of(user, LANGS)) + f"\n\nID: {inc['id']}",
                MessageAttributes={"owner": {"DataType": "String", "StringValue": owner}})
        if os.environ.get("NOTIFY_TEAM_ALL", "false").lower() != "true":
            return
    topic_arn = os.environ.get("SNS_TOPIC_ARN")
    if not topic_arn:
        print("SNS_TOPIC_ARN не задан, пропускаю")
        return
    boto3.client("sns").publish(
        TopicArn=topic_arn, Subject=subject,
        Message=all_languages(inc, icons=False) + f"\n\nID: {inc['id']}",
    )


RESOLVED = {
    "ru": "✅ Решено: {title}\nСервер: {server}\nДлилось: {minutes} мин.",
    "en": "✅ Resolved: {title}\nServer: {server}\nLasted: {minutes} min.",
}


def notify_resolved(inc, ok_time):
    """Короткое сообщение в Telegram, что инцидент закрылся (аларм вернулся в OK)."""
    try:
        start = datetime.fromisoformat(str(inc["timestamp"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(ok_time.replace("Z", "+00:00"))
        minutes = max(1, round((end - start).total_seconds() / 60))
    except Exception:
        minutes = "?"
    for chat_id, langs in recipients.telegram_targets(inc.get("server_id"), LANGS):
        text = "\n\n— — —\n\n".join(
            RESOLVED[lang].format(title=tr(inc, "title", lang), server=inc.get("server", ""), minutes=minutes)
            for lang in langs)
        try:
            telegram(text, chat_id)
        except Exception as e:
            print(f"telegram {chat_id}: {e}")


# ---------- Основная функция ----------
 
def attach_to_open(open_inc, incident):
    """Добавляет аларм к уже открытому инциденту сервера. Шлёт короткое сообщение,
    только если новый аларм серьёзнее."""
    table_name = os.environ.get("TABLE_NAME")
    related = {"alarm_name": incident["alarm_name"], "alarm_type": incident["alarm_type"],
               "title": incident["title"], "severity": incident["severity"],
               "timestamp": incident["timestamp"]}
    worse = (SEVERITY_RANK.get(incident["severity"], 1) > SEVERITY_RANK.get(open_inc.get("severity"), 1)
             or (incident.get("alarm_type") in ("app-down", "server-down")
                 and incident.get("alarm_type") != open_inc.get("alarm_type")))
    if table_name:
        expr = "SET related_alarms = list_append(if_not_exists(related_alarms, :empty), :r)"
        values = {":r": [related], ":empty": []}
        if worse:
            expr += ", severity = :s"
            values[":s"] = incident["severity"]
        dynamodb.Table(table_name).update_item(
            Key={"incident_id": open_inc["incident_id"]},
            UpdateExpression=expr, ExpressionAttributeValues=values)
    if worse:
        icon = {"high": "🟠", "critical": "🔴"}.get(incident["severity"], "🟡")
        for chat_id, langs in recipients.telegram_targets(incident.get("server_id"), LANGS):
            blocks = []
            for lang in langs:
                t = L[lang]
                blocks.append(f"{icon} {t['worse'].format(server=incident['server'])}: {tr(incident, 'title', lang)}\n"
                              f"{tr(incident, 'root_cause', lang)}\n"
                              + t["cont"].format(title=tr(open_inc, "title", lang)))
            try:
                telegram("\n\n— — —\n\n".join(blocks), chat_id)
            except Exception as e:
                print(f"telegram {chat_id}: {e}")
    print(json.dumps({"status": "grouped", "into": open_inc["incident_id"],
                      "alarm": incident["alarm_name"], "escalated": worse}, ensure_ascii=False))
    return {"status": "grouped", "id": open_inc["incident_id"], "escalated": worse}


def lambda_handler(event, context):
    detail = event["detail"]
    state = detail["state"]["value"]

    if state == "OK":
        return resolve_incidents(detail["alarmName"], event["time"])
    if state != "ALARM":
        return {"skipped": state}

    reason_data = json.loads(detail["state"]["reasonData"])
    metric = detail["configuration"]["metrics"][0]["metricStat"]["metric"]

    dims = metric.get("dimensions") or {}
    # Тревога сервера с агентом Kezekshi (ServerId) или старая тревога демо-сервера (InstanceId)
    server_id = dims.get("ServerId")
    raw_logs = get_logs(event["time"], server_id=server_id)
    # Если в тревоге несколько точек, берём пиковую, а не самую раннюю
    value = max(reason_data.get("recentDatapoints") or [0])
    if server_id:
        # имя из таблицы серверов (его можно поменять), а не из описания аларма
        try:
            server = recipients.server_item(server_id).get("name") or ""
        except Exception as e:
            print(f"server name: {e!r}")
            server = ""
        server = server or server_name(detail["configuration"].get("description"), server_id)
        series = get_server_series(event["time"], server_id)
        stats = stats_from_series(series)
        # автопочинка только на демо-сервере, на серверах клиентов мы ничего не запускаем
        instance_id = os.environ.get("INSTANCE_ID", "") if server_id == DEMO_SERVER_ID else ""
    else:
        instance_id = dims.get("InstanceId") or os.environ.get("INSTANCE_ID", "unknown")
        server = SERVICE
        series = {}
        stats = get_stats(event["time"], instance_id)
    server_key = server_id or f"instance:{instance_id}"

    # id зависит от тревоги и момента её перехода в ALARM. Повторная доставка того же
    # события даёт тот же id (дубль отсекается), а новая тревога получает новый id,
    # даже если она случилась через пару минут после починки.
    now = int(time.time())
    state_ts = detail["state"].get("timestamp") or event.get("id") or str(now)
    inc_id = "inc-" + hashlib.sha1(f'{detail["alarmName"]}:{state_ts}'.encode()).hexdigest()[:8]
    log_summary = summarize_logs(raw_logs)
    alarm_time_ts = datetime.fromisoformat(event["time"].replace("Z", "+00:00")).timestamp()

    analysis = explain.explain(detail["alarmName"], float(value), reason_data["threshold"],
                               series, log_summary, alarm_time_ts)
    incident = {
        "id": inc_id,
        "status": "open",
        "alarm_name": detail["alarmName"],
        "alarm_type": analysis["alarm_type"],
        "timestamp": event["time"],
        "region": event["region"],
        "instance_id": instance_id,
        "server_id": server_id or "",
        "server_key": server_key,
        "server": server,
        "metric": f'{metric["namespace"]}/{metric["name"]}',
        "value": value,
        "threshold": reason_data["threshold"],
        "reason": detail["state"]["reason"],
        "stats": stats,
        "logs_sample": raw_logs[-10:],
        "log_summary": log_summary,
    }
    incident.update(sanitize(analysis))
    incident.update({
        "title_en": analysis.get("title_en", ""),
        "root_cause_en": analysis.get("root_cause_en", ""),
        "recommendation_steps_en": analysis.get("recommendation_steps_en", [])[:4],
        "cause_code": analysis["cause_code"],
        "cause_source": analysis["cause_source"],
        "load_ratio": analysis.get("load_ratio"),
        "explained_by": analysis["cause_source"],
    })

    # Тот же сервер уже в инциденте: не плодим повторы
    open_inc = open_incident_for(server_key, event["time"])
    if open_inc and open_inc.get("incident_id") != inc_id:
        return attach_to_open(open_inc, incident)

    incident["forecast_warning"] = forecast_warning(server_id, analysis["alarm_type"], event["time"])
    incident["history"] = similar_history(server_key, analysis["alarm_type"], event["time"])
    incident["ttl"] = now + 7 * 24 * 3600  # DynamoDB удалит запись через 7 дней

    try:
        is_new = save_incident(incident)
    except Exception as e:
        print(f"запись в DynamoDB не удалась: {e}")
        incident["save_error"] = str(e)
        is_new = True  # уведомление всё равно отправляем

    if not is_new:
        print("дубль, уведомления не отправляю:", inc_id)
        return {"status": "duplicate", "id": inc_id}

    # Автопочинка только для нового инцидента
    incident["auto_fix"] = run_auto_fix(incident)
    try:
        save_auto_fix(inc_id, incident["auto_fix"])
    except Exception as e:
        print(f"auto_fix не записался: {e}")

    for notify in (send_telegram, send_sns):
        try:
            notify(incident)
        except Exception as e:
            print(f"{notify.__name__} failed: {e}")

    print(json.dumps(incident, ensure_ascii=False, default=str))
    return incident
