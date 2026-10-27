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
from botocore.exceptions import ClientError

LOG_GROUP = "/demoapp/app"
GEMINI_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
]
ALLOWED_SEVERITY = {"low", "medium", "high", "critical"}

logs_client = boto3.client("logs")
cw_client = boto3.client("cloudwatch")
dynamodb = boto3.resource("dynamodb")


# ---------- Логи ----------

def get_logs(alarm_time, minutes=10, max_lines=50):
    """Берёт строки логов за N минут до тревоги."""
    end = datetime.fromisoformat(alarm_time.replace("Z", "+00:00"))
    start = end - timedelta(minutes=minutes)
    try:
        resp = logs_client.filter_log_events(
            logGroupName=LOG_GROUP,
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


# ---------- Статистика: CPU и диск ----------

def last_value(namespace, name, dimensions, end, minutes=20, stat="Average"):
    """Последнее значение метрики за N минут или None, если данных нет."""
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
        return round(points[-1][stat], 1) if points else None
    except Exception as e:
        print(f"метрика {name} не прочиталась: {e}")
        return None


def get_stats(alarm_time, instance_id, errors):
    """errors_per_min, cpu, disk. Чего нет, там None."""
    end = datetime.fromisoformat(alarm_time.replace("Z", "+00:00"))
    stats = {"errors_per_min": errors, "cpu": None, "disk": None}
    if not instance_id or instance_id == "unknown":
        return stats

    inst_dim = [{"Name": "InstanceId", "Value": instance_id}]
    stats["cpu"] = last_value("AWS/EC2", "CPUUtilization", inst_dim, end)

    # Диск шлёт CloudWatch agent, у метрики есть доп. измерения (path, device),
    # поэтому сначала находим её точное описание
    try:
        found = cw_client.list_metrics(
            Namespace="CWAgent",
            MetricName="disk_used_percent",
            Dimensions=inst_dim,
        ).get("Metrics", [])
        if found:
            root = [m for m in found if {"Name": "path", "Value": "/"} in m["Dimensions"]]
            chosen = (root or found)[0]
            stats["disk"] = last_value("CWAgent", "disk_used_percent", chosen["Dimensions"], end)
    except Exception as e:
        print(f"диск не прочитался: {e}")
    return stats


# ---------- Gemini ----------

def call_model(model, api_key, body):
    """Один запрос к одной модели, с одной повторной попыткой при перегрузке."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    for attempt in range(2):
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt == 0:
                time.sleep(2)
                continue
            detail = e.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"HTTP {e.code}: {detail}")


def ask_gemini(incident):
    """Возвращает (разбор, имя_модели, ошибка)."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, None, "не задана переменная GEMINI_API_KEY"

    context = {
        "alarm": incident["alarm_name"],
        "metric": incident["metric"],
        "value": incident["value"],
        "threshold": incident["threshold"],
        "time_utc": incident["timestamp"],
        "stats": incident["stats"],
        "log_summary": incident["log_summary"],
        "last_log_lines": incident["logs_sample"][-5:],
    }
    prompt = (
        "Ты дежурный DevOps-инженер. Сработала тревога на сервере.\n"
        "Контекст: приложение DemoApp (Python, systemd-сервис demo-app) "
        "на одном EC2-сервере. Относись к инциденту как к реальному и не "
        "упоминай, что это демо или симуляция, даже если такие слова есть в логах: "
        "вместо этого пиши «в логах зафиксирован сбой». "
        "Реально доступные действия: проверить статус и логи systemd-сервиса, "
        "перезапустить сервис, проверить CPU и диск, очистить временные файлы. "
        "Не предлагай то, чего в этом окружении нет.\n"
        "В stats значение null означает, что данных нет, не считай это проблемой. "
        "Оценивай CPU и диск: высокий CPU (больше 85) или диск (больше 90) может быть причиной.\n\n"
        "Ответь СТРОГО JSON-объектом с полями:\n"
        '"title" (короткий заголовок, до 80 символов),\n'
        '"severity" (ровно одно из: low, medium, high, critical),\n'
        '"root_cause" (вероятная причина, 1-2 предложения),\n'
        '"recommendation_steps" (массив из 2-4 коротких конкретных шагов),\n'
        '"auto_fix_possible" (true, только если хватит перезапуска сервиса или очистки диска; иначе false).\n'
        "Пиши по-русски, простыми словами, без выдумок: опирайся только на данные.\n\n"
        f"Данные:\n{json.dumps(context, ensure_ascii=False)}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
            "maxOutputTokens": 2000,
        },
    }

    errors = []
    for model in GEMINI_MODELS:
        try:
            data = call_model(model, api_key, body)
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text), model, None
        except Exception as e:
            errors.append(f"{model}: {e}")
    return None, None, " | ".join(errors)


def sanitize(ai):
    """Сайт всегда получает значения из допустимого списка."""
    if not isinstance(ai, dict):
        ai = {}
    sev = str(ai.get("severity", "")).lower()
    steps = ai.get("recommendation_steps")
    if not isinstance(steps, list) or not steps:
        steps = ["Проверить статус сервиса demo-app", "Посмотреть логи приложения"]
    return {
        "title": str(ai.get("title") or "Инцидент")[:120],
        "severity": sev if sev in ALLOWED_SEVERITY else "medium",
        "root_cause": str(ai.get("root_cause") or "Причина не определена"),
        "recommendation_steps": [str(s) for s in steps][:4],
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


# ---------- Уведомления ----------

def fmt(v):
    """Красиво показывает процент или «нет данных»."""
    return "нет данных" if v is None else f"{v}%"


def stats_line(st):
    errors = st.get("errors_per_min")
    errors_text = "нет данных" if errors is None else int(errors)
    return (
        f"CPU: {fmt(st.get('cpu'))}  "
        f"Диск: {fmt(st.get('disk'))}  "
        f"Ошибок/мин: {errors_text}"
    )


def send_telegram(inc):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram не настроен, пропускаю")
        return
    icons = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}
    text = (
        f"{icons.get(inc['severity'], '⚪')} {inc['title']}\n"
        f"Важность: {inc['severity']}\n"
        f"{stats_line(inc['stats'])}\n"
        f"Причина: {inc['root_cause']}\n"
        f"Первый шаг: {inc['recommendation_steps'][0]}"
    )
    data = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10).read()


def send_sns(inc):
    topic_arn = os.environ.get("SNS_TOPIC_ARN")
    if not topic_arn:
        print("SNS_TOPIC_ARN не задан, пропускаю")
        return
    steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(inc["recommendation_steps"]))
    message = (
        f"[{inc['severity'].upper()}] {inc['title']}\n\n"
        f"{stats_line(inc['stats'])}\n\n"
        f"{inc['root_cause']}\n\n"
        f"Что делать:\n{steps}\n\n"
        f"ID: {inc['id']}"
    )
    boto3.client("sns").publish(
        TopicArn=topic_arn,
        Subject=f"[{inc['severity'].upper()}] {inc['title']}"[:100],
        Message=message,
    )


# ---------- Основная функция ----------

def lambda_handler(event, context):
    detail = event["detail"]
    state = detail["state"]["value"]

    if state != "ALARM":
        return {"skipped": state}

    reason_data = json.loads(detail["state"]["reasonData"])
    metric = detail["configuration"]["metrics"][0]["metricStat"]["metric"]

    raw_logs = get_logs(event["time"])
    value = reason_data["recentDatapoints"][0]
    instance_id = os.environ.get("INSTANCE_ID", "unknown")

    # id зависит от тревоги и 10-минутного окна: повторная тревога даёт тот же id
    now = int(time.time())
    window = now // 600
    inc_id = "inc-" + hashlib.sha1(f'{detail["alarmName"]}:{window}'.encode()).hexdigest()[:8]

    incident = {
        "id": inc_id,
        "status": "open",
        "alarm_name": detail["alarmName"],
        "timestamp": event["time"],
        "region": event["region"],
        "instance_id": instance_id,
        "metric": f'{metric["namespace"]}/{metric["name"]}',
        "value": value,
        "threshold": reason_data["threshold"],
        "reason": detail["state"]["reason"],
        "stats": get_stats(event["time"], instance_id, value),
        "logs_sample": raw_logs[-10:],
        "log_summary": summarize_logs(raw_logs),
    }

    ai, model, error = ask_gemini(incident)
    incident.update(sanitize(ai))
    if ai:
        incident["ai_model"] = model
    else:
        incident["ai_error"] = error  # инцидент создаётся и без разбора ИИ
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

    for notify in (send_telegram, send_sns):
        try:
            notify(incident)
        except Exception as e:
            print(f"{notify.__name__} failed: {e}")

    print(json.dumps(incident, ensure_ascii=False))
    return incident