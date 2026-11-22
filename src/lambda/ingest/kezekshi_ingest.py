"""POST /ingest: принимает данные от агента Kezekshi (src/agent/kezekshi_agent.py) раз в минуту.

Заголовок X-Kezekshi-Key: ключ из /register.
Тело: {"server", "time", "cpu", "mem", "disk", "errors": [...], "error_count",
       агент v2 ещё: "log_lines", "auth_failures", "not_found", "latency_p95_ms",
       "health_ok", "response_ms"}
Ответ 200: {"ok": true, "server_id": "..."};  401: неизвестный ключ;  400: не JSON.

Что делает:
1. Проверяет ключ и отмечает, что сервер на связи (status = online, last_seen).
2. Пишет метрики cpu, mem, disk, error_count и heartbeat в CloudWatch
   (namespace METRICS_NAMESPACE, измерение ServerId). На них висят алармы из /register.
3. Пишет строки errors в лог-группу SERVERS_LOG_GROUP, поток = server_id.
   Оттуда incident-analyzer берёт контекст для разбора.
4. Создаёт алармы сервера с префиксом ALARM_PREFIX (их ловит общее правило EventBridge).
   Какие алармы нужны, зависит от того, что присылает агент (см. wanted_alarms):
   у агента v1 их 5, у агента v2 с проверкой приложения до 9. Недостающие
   досоздаются автоматически, когда сервер начинает присылать новые поля.
   Алармы создаются здесь, а не в /register: так не остаётся алармов у серверов,
   которые зарегистрировали, но так и не подключили.
Если CloudWatch временно недоступен, агенту всё равно отвечаем 200: данные следующей
минуты придут сами, а ретраи агента только создадут лишнюю нагрузку.
"""
import base64
import hashlib
import json
import os
import time
from datetime import datetime, timezone

import boto3

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
CW = boto3.client("cloudwatch")
LOGS = boto3.client("logs")
NAMESPACE = os.environ.get("METRICS_NAMESPACE", "Kezekshi")
LOG_GROUP = os.environ.get("SERVERS_LOG_GROUP", "/kezekshi/servers")
ALARM_PREFIX = os.environ.get("ALARM_PREFIX", "ai-oncall-engineer-")
MAX_ERRORS = 20


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def read_body(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("body is not a JSON object")
    return data


def number(value, low=0.0, high=None):
    """Число из тела агента или None, если пришло что-то странное."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if value < low or (high is not None and value > high):
        return None
    return value


# поле агента -> (метрика, единица, максимум)
FIELDS = (
    ("cpu", "CpuPercent", "Percent", 100.0),
    ("mem", "MemPercent", "Percent", 100.0),
    ("disk", "DiskPercent", "Percent", 100.0),
    ("error_count", "ErrorCount", "Count", None),
    ("log_lines", "LogLines", "Count", None),
    ("auth_failures", "AuthFailures", "Count", None),
    ("not_found", "NotFound", "Count", None),
    ("latency_p95_ms", "LatencyP95", "Milliseconds", None),
    ("health_ok", "HealthOk", "None", 1.0),
    ("response_ms", "ResponseMs", "Milliseconds", None),
)


def put_metrics(server_id, data):
    dims = [{"Name": "ServerId", "Value": server_id}]
    metrics = [{"MetricName": "Heartbeat", "Dimensions": dims, "Value": 1.0, "Unit": "Count"}]
    for field, name, unit, high in FIELDS:
        value = number(data.get(field), high=high)
        if value is not None:
            metrics.append({"MetricName": name, "Dimensions": dims, "Value": value, "Unit": unit})
    CW.put_metric_data(Namespace=NAMESPACE, MetricData=metrics)


def put_errors(server_id, errors):
    lines = [str(e)[:2000] for e in errors if str(e).strip()][-MAX_ERRORS:]
    if not lines:
        return
    try:
        LOGS.create_log_stream(logGroupName=LOG_GROUP, logStreamName=server_id)
    except LOGS.exceptions.ResourceAlreadyExistsException:
        pass
    now_ms = int(time.time() * 1000)
    LOGS.put_log_events(
        logGroupName=LOG_GROUP,
        logStreamName=server_id,
        logEvents=[{"timestamp": now_ms, "message": line} for line in lines],
    )


DOWN_ALARM_DELAY = 300  # секунд после первого контакта до создания server-down


# суффикс -> (метрика, статистика, порог, оператор, периодов подряд, нет данных, описание)
ALARMS = {
    "cpu-high": ("CpuPercent", "Average", 80, "GreaterThanThreshold", 2, "notBreaching",
                 "CPU above 80% on {name}"),
    "mem-high": ("MemPercent", "Average", 90, "GreaterThanThreshold", 3, "notBreaching",
                 "Memory above 90% for 3 minutes on {name}"),
    "disk-high": ("DiskPercent", "Maximum", 85, "GreaterThanThreshold", 1, "notBreaching",
                  "Disk above 85% on {name}"),
    "app-errors": ("ErrorCount", "Sum", 5, "GreaterThanThreshold", 1, "notBreaching",
                   "More than 5 errors per minute in logs on {name}"),
    "server-down": ("Heartbeat", "SampleCount", 1, "LessThanThreshold", 3, "breaching",
                    "No data from agent for 3 minutes on {name}"),
    "app-down": ("HealthOk", "Average", 0.5, "LessThanThreshold", 2, "notBreaching",
                 "Application does not respond to health check for 2 minutes on {name}"),
    "slow-responses": (None, "Average", 2000, "GreaterThanThreshold", 3, "notBreaching",
                       "Responses slower than 2 s for 3 minutes on {name}"),
    "auth-attack": ("AuthFailures", "Sum", 60, "GreaterThanThreshold", 2, "notBreaching",
                    "More than 60 failed logins (401/403) per minute on {name}"),
    "log-flood": ("LogLines", "Sum", 12000, "GreaterThanThreshold", 3, "notBreaching",
                  "More than 12000 log lines per minute on {name}"),
}


def wanted_alarms(data, first_seen, now):
    """Какие алармы нужны серверу, судя по тому, что прислал агент."""
    want = {"cpu-high", "mem-high", "disk-high", "app-errors"}
    if now - first_seen >= DOWN_ALARM_DELAY:
        want.add("server-down")
    if data.get("health_ok") is not None:
        want.add("app-down")
    if data.get("latency_p95_ms") is not None or data.get("response_ms") is not None:
        want.add("slow-responses")
    if data.get("auth_failures") is not None:
        want.add("auth-attack")
    if data.get("log_lines") is not None:
        want.add("log-flood")
    return want


def create_alarm(server_id, name, suffix, data):
    """PutMetricAlarm перезаписывает аларм с тем же именем, повтор безопасен.
    server-down создаётся только через DOWN_ALARM_DELAY после первого контакта:
    иначе у нового сервера нет данных за прошлые минуты, и аларм ложно уходит в ALARM."""
    metric, stat, threshold, op, periods, missing, description = ALARMS[suffix]
    if suffix == "slow-responses":
        # задержка реальных запросов из логов точнее, чем одна проверка в 20 секунд
        metric = "LatencyP95" if data.get("latency_p95_ms") is not None else "ResponseMs"
    CW.put_metric_alarm(
        AlarmName=f"{ALARM_PREFIX}srv-{server_id}-{suffix}",
        AlarmDescription=description.format(name=name),
        Namespace=NAMESPACE,
        MetricName=metric,
        Dimensions=[{"Name": "ServerId", "Value": server_id}],
        Statistic=stat,
        Period=60,
        EvaluationPeriods=periods,
        DatapointsToAlarm=periods,
        Threshold=threshold,
        ComparisonOperator=op,
        TreatMissingData=missing,
        Tags=[{"Key": "ServerId", "Value": server_id}, {"Key": "ServerName", "Value": name}],
    )


def created_alarms(server):
    done = set(server.get("alarm_set") or [])
    # серверы, подключённые до v4, отмечены старыми флагами
    if server.get("alarms_created"):
        done |= {"cpu-high", "disk-high", "app-errors"}
    if server.get("down_alarm_created"):
        done.add("server-down")
    return done


def lambda_handler(event, context):
    # HTTP API передаёт имена заголовков в нижнем регистре
    key = (event.get("headers") or {}).get("x-kezekshi-key", "")
    if not key:
        return response(401, {"error": "Нет заголовка X-Kezekshi-Key"})

    key_hash = hashlib.sha256(key.encode()).hexdigest()
    server = TABLE.get_item(Key={"key_hash": key_hash}).get("Item")
    if not server:
        return response(401, {"error": "Неизвестный ключ"})

    try:
        data = read_body(event)
    except (ValueError, UnicodeDecodeError):
        return response(400, {"error": "Тело запроса должно быть JSON-объектом"})
    data.pop("key", None)  # агент дублирует ключ в теле, в логи он попасть не должен

    # Первый контакт снимает ttl: зарегистрированный сервер больше не удаляется
    TABLE.update_item(
        Key={"key_hash": key_hash},
        UpdateExpression="SET #s = :online, last_seen = :now REMOVE #ttl",
        ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
        ExpressionAttributeValues={
            ":online": "online",
            ":now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )

    server_id = server["server_id"]
    errors = data.get("errors") if isinstance(data.get("errors"), list) else []
    problems = []
    now = int(time.time())
    try:
        first_seen = int(server.get("first_seen") or now)
        done = created_alarms(server)
        missing = wanted_alarms(data, first_seen, now) - done
        for suffix in sorted(missing):
            create_alarm(server_id, server["name"], suffix, data)
        if missing or not server.get("first_seen"):
            TABLE.update_item(
                Key={"key_hash": key_hash},
                UpdateExpression="SET alarm_set = :a, first_seen = if_not_exists(first_seen, :now)",
                ExpressionAttributeValues={":a": sorted(done | missing), ":now": now},
            )
    except Exception as exc:  # попробуем ещё раз со следующей минутой
        problems.append(f"alarms: {exc}")
    try:
        put_metrics(server_id, data)
    except Exception as exc:
        problems.append(f"metrics: {exc}")
    try:
        put_errors(server_id, errors)
    except Exception as exc:
        problems.append(f"logs: {exc}")

    print(json.dumps({
        "server_id": server_id, "name": server["name"],
        "cpu": data.get("cpu"), "mem": data.get("mem"), "disk": data.get("disk"),
        "error_count": data.get("error_count"), "health_ok": data.get("health_ok"),
        "agent_version": data.get("agent_version", 1), "problems": problems,
    }, ensure_ascii=False))
    return response(200, {"ok": True, "server_id": server_id})
