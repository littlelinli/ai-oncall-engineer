"""POST /ingest: принимает данные от агента Kezekshi (src/agent/kezekshi_agent.py) раз в минуту.

Заголовок X-Kezekshi-Key: ключ из /register.
Тело: {"server", "time", "cpu", "mem", "disk", "errors": [...], "error_count"}
Ответ 200: {"ok": true, "server_id": "..."};  401: неизвестный ключ;  400: не JSON.

Сейчас Lambda проверяет ключ и отмечает, что сервер на связи (status = online, last_seen).
TODO (#3): метрики cpu/mem/disk/error_count -> CloudWatch (namespace METRICS_NAMESPACE,
измерение ServerId), строки errors -> лог-группа SERVERS_LOG_GROUP. Права в IAM уже выданы.
"""
import base64
import hashlib
import json
import os
from datetime import datetime, timezone

import boto3

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


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

    print(json.dumps({"server_id": server["server_id"], "name": server["name"], "data": data}, ensure_ascii=False))
    return response(200, {"ok": True, "server_id": server["server_id"]})
