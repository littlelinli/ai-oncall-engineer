"""POST /ingest: принимает данные от агента Kezekshi (src/agent/kezekshi_agent.py) раз в минуту.

Заголовок X-Kezekshi-Key: ключ из /register.
Тело: {"server", "time", "cpu", "mem", "disk", "errors": [...], "error_count"}
Ответ 200: {"ok": true, "server_id": "..."}; 401: неизвестный ключ; 400: не JSON.

Что делает: проверяет ключ, ставит status=online, пишет cpu/mem/disk/error_count
в CloudWatch (namespace Kezekshi, измерение ServerId), строки errors пишет
в лог-группу /kezekshi/servers (стрим = server_id).
При первом контакте создаёт алармы сервера ai-oncall-engineer-<name>-<server_id>-*:
так алармы появляются только у серверов, где агент реально установлен.
"""
import base64
import hashlib
import json
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
CW = boto3.client("cloudwatch")
LOGS = boto3.client("logs")

NAMESPACE = os.environ.get("METRICS_NAMESPACE", "Kezekshi")
LOG_GROUP = os.environ.get("SERVERS_LOG_GROUP", "/kezekshi/servers")
ALARM_PREFIX = os.environ.get("ALARM_PREFIX", "ai-oncall-engineer-")
CPU_THRESHOLD = float(os.environ.get("CPU_THRESHOLD", "85"))
DISK_THRESHOLD = float(os.environ.get("DISK_THRESHOLD", "90"))

MAX_ERROR_LINES = 100
MAX_LINE_LEN = 8000
_streams_ready = set()  # кэш между вызовами в тёплой Lambda

METRICS = [  # (поле в теле, имя метрики, единица)
    ("cpu", "cpu", "Percent"),
    ("mem", "mem", "Percent"),
    ("disk", "disk", "Percent"),
    ("error_count", "error_count", "Count"),
]


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


def put_metrics(server_id, data):
    dims = [{"Name": "ServerId", "Value": server_id}]
    items = []
    for field, metric, unit in METRICS:
        try:
            value = float(data[field])
        except (KeyError, TypeError, ValueError):
            continue
        if value != value or value in (float("inf"), float("-inf")):
            continue
        items.append({
            "MetricName": metric,
            "Dimensions": dims,
            "Value": value,
            "Unit": unit,
        })
    if items:
        CW.put_metric_data(Namespace=NAMESPACE, MetricData=items)


def create_alarms(server_id, name):
    dims = [{"Name": "ServerId", "Value": server_id}]
    base = f"{ALARM_PREFIX}{name}-{server_id}"
    common = dict(Namespace=NAMESPACE, Dimensions=dims, Period=60)

    alarms = [
        dict(
            AlarmName=f"{base}-cpu",
            AlarmDescription=f"Сервер {name} ({server_id}): высокая загрузка CPU",
            MetricName="cpu", Statistic="Average",
            ComparisonOperator="GreaterThanThreshold", Threshold=CPU_THRESHOLD,
            EvaluationPeriods=3, DatapointsToAlarm=3,
            TreatMissingData="notBreaching", **common,
        ),
        dict(
            AlarmName=f"{base}-disk",
            AlarmDescription=f"Сервер {name} ({server_id}): диск почти заполнен",
            MetricName="disk", Statistic="Average",
            ComparisonOperator="GreaterThanThreshold", Threshold=DISK_THRESHOLD,
            EvaluationPeriods=2, DatapointsToAlarm=2,
            TreatMissingData="notBreaching", **common,
        ),
        dict(
            AlarmName=f"{base}-errors",
            AlarmDescription=f"Сервер {name} ({server_id}): ошибки в логе приложения",
            MetricName="error_count", Statistic="Sum",
            ComparisonOperator="GreaterThanThreshold", Threshold=0,
            EvaluationPeriods=1, DatapointsToAlarm=1,
            TreatMissingData="notBreaching", **common,
        ),
        dict(
            # агент шлёт данные раз в минуту; нет ни одной точки 3 минуты подряд = сервер пропал
            AlarmName=f"{base}-down",
            AlarmDescription=f"Сервер {name} ({server_id}): нет данных от агента 3 минуты",
            MetricName="cpu", Statistic="SampleCount",
            ComparisonOperator="LessThanThreshold", Threshold=1,
            EvaluationPeriods=3, DatapointsToAlarm=3,
            TreatMissingData="breaching", **common,
        ),
    ]
    for alarm in alarms:
        CW.put_metric_alarm(**alarm)


def ensure_stream(stream):
    if stream in _streams_ready:
        return
    try:
        LOGS.create_log_stream(logGroupName=LOG_GROUP, logStreamName=stream)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    _streams_ready.add(stream)


def put_errors(server_id, name, data):
    errors = data.get("errors")
    if not isinstance(errors, list) or not errors:
        return
    now_ms = int(time.time() * 1000)
    events = []
    for i, line in enumerate(errors[:MAX_ERROR_LINES]):
        message = json.dumps(
            {"server_id": server_id, "server": name, "error": str(line)[:MAX_LINE_LEN]},
            ensure_ascii=False,
        )
        events.append({"timestamp": now_ms + i, "message": message})
    ensure_stream(server_id)
    LOGS.put_log_events(logGroupName=LOG_GROUP, logStreamName=server_id, logEvents=events)


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

    server_id = server["server_id"]

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

    # Ошибки CloudWatch не должны ронять приём данных от агента
    try:
        put_metrics(server_id, data)
    except Exception as e:
        print(json.dumps({"server_id": server_id, "metrics_error": str(e)}, ensure_ascii=False))
    try:
        put_errors(server_id, server["name"], data)
    except Exception as e:
        print(json.dumps({"server_id": server_id, "logs_error": str(e)}, ensure_ascii=False))

    # Алармы создаются после первых метрик, иначе -down сразу уйдёт в ALARM.
    # Если не получилось, флаг не ставится и попытка повторится через минуту.
    if not server.get("alarms_created"):
        try:
            create_alarms(server_id, server["name"])
            TABLE.update_item(
                Key={"key_hash": key_hash},
                UpdateExpression="SET alarms_created = :t",
                ExpressionAttributeValues={":t": True},
            )
        except Exception as e:
            print(json.dumps({"server_id": server_id, "alarms_error": str(e)}, ensure_ascii=False))

    return response(200, {"ok": True, "server_id": server_id})