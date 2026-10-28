"""POST /ingest: принимает данные от агента Kezekshi (src/agent/kezekshi_agent.py) раз в минуту.

Заголовок X-Kezekshi-Key: ключ из /register.
Тело: {"server", "time", "cpu", "mem", "disk", "errors": [...], "error_count"}
Ответ 200: {"ok": true, "server_id": "..."};  401: неизвестный ключ;  400: не JSON.

Что делает:
1. Проверяет ключ и отмечает, что сервер на связи (status = online, last_seen).
2. Пишет метрики cpu, mem, disk, error_count и heartbeat в CloudWatch
   (namespace METRICS_NAMESPACE, измерение ServerId). На них висят алармы из /register.
3. Пишет строки errors в лог-группу SERVERS_LOG_GROUP, поток = server_id.
   Оттуда incident-analyzer берёт контекст для разбора.
4. При первом контакте сервера создаёт для него 4 аларма с префиксом ALARM_PREFIX
   (CPU, диск, ошибки, «сервер пропал»). Их ловит общее правило EventBridge.
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


def put_metrics(server_id, data):
    dims = [{"Name": "ServerId", "Value": server_id}]
    metrics = [{"MetricName": "Heartbeat", "Dimensions": dims, "Value": 1.0, "Unit": "Count"}]
    for field, name, unit, high in (
        ("cpu", "CpuPercent", "Percent", 100.0),
        ("mem", "MemPercent", "Percent", 100.0),
        ("disk", "DiskPercent", "Percent", 100.0),
        ("error_count", "ErrorCount", "Count", None),
    ):
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


def create_alarms(server_id, name):
    """4 аларма на сервер. PutMetricAlarm перезаписывает аларм с тем же именем,
    поэтому повторный вызов безопасен."""
    dims = [{"Name": "ServerId", "Value": server_id}]
    base = f"{ALARM_PREFIX}srv-{server_id}"
    alarms = [
        # имя, метрика, статистика, порог, оператор, периодов, нет данных
        ("cpu-high", "CpuPercent", "Average", 80, "GreaterThanThreshold", 2, "notBreaching",
         f"CPU above 80% on {name}"),
        ("disk-high", "DiskPercent", "Maximum", 85, "GreaterThanThreshold", 1, "notBreaching",
         f"Disk above 85% on {name}"),
        ("app-errors", "ErrorCount", "Sum", 5, "GreaterThanThreshold", 1, "notBreaching",
         f"More than 5 errors per minute in logs on {name}"),
        ("server-down", "Heartbeat", "SampleCount", 1, "LessThanThreshold", 3, "breaching",
         f"No data from agent for 3 minutes on {name}"),
    ]
    for suffix, metric, stat, threshold, op, periods, missing, description in alarms:
        CW.put_metric_alarm(
            AlarmName=f"{base}-{suffix}",
            AlarmDescription=description,
            Namespace=NAMESPACE,
            MetricName=metric,
            Dimensions=dims,
            Statistic=stat,
            Period=60,
            EvaluationPeriods=periods,
            DatapointsToAlarm=periods,
            Threshold=threshold,
            ComparisonOperator=op,
            TreatMissingData=missing,
            Tags=[{"Key": "ServerId", "Value": server_id}, {"Key": "ServerName", "Value": name}],
        )


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
    try:
        put_metrics(server_id, data)
    except Exception as exc:
        problems.append(f"metrics: {exc}")
    try:
        put_errors(server_id, errors)
    except Exception as exc:
        problems.append(f"logs: {exc}")
    # Алармы после первых метрик: иначе server-down может сразу увидеть «нет данных»
    if not server.get("alarms_created"):
        try:
            create_alarms(server_id, server["name"])
            TABLE.update_item(
                Key={"key_hash": key_hash},
                UpdateExpression="SET alarms_created = :t",
                ExpressionAttributeValues={":t": True},
            )
        except Exception as exc:  # попробуем ещё раз со следующей минутой
            problems.append(f"alarms: {exc}")

    print(json.dumps({
        "server_id": server_id, "name": server["name"],
        "cpu": data.get("cpu"), "mem": data.get("mem"), "disk": data.get("disk"),
        "error_count": data.get("error_count"), "problems": problems,
    }, ensure_ascii=False))
    return response(200, {"ok": True, "server_id": server_id})