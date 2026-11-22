"""POST /register: регистрирует новый сервер и возвращает команду установки агента.

Запрос (тело необязательно):  {"name": "shop-api-prod"}
Ответ 200: {"server_id", "name", "key", "ingest_url", "install_url", "command"}
Ответ 400: {"error": "..."}

В таблице хранится только sha256 от ключа, сам ключ показывается один раз.
Если сервер не вышел на связь за сутки, запись удаляется по ttl.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone

import boto3

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
INSTALL_URL = os.environ["INSTALL_URL"]
INGEST_URL = os.environ["INGEST_URL"]
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,63}$")  # как в install.sh
PENDING_TTL = 24 * 3600


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
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("body is not a JSON object")
    return data


def lambda_handler(event, context):
    try:
        data = read_body(event)
    except (ValueError, UnicodeDecodeError):
        return response(400, {"error": "Тело запроса должно быть JSON-объектом"})

    server_id = secrets.token_hex(4)
    name = str(data.get("name") or f"server-{server_id}").strip()
    if not NAME_RE.match(name):
        return response(400, {"error": "name: только буквы, цифры, точка, _ и -, до 63 символов"})

    key = "kz_" + secrets.token_urlsafe(24)
    TABLE.put_item(
        Item={
            "key_hash": hashlib.sha256(key.encode()).hexdigest(),
            "server_id": server_id,
            "name": name,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ttl": int(time.time()) + PENDING_TTL,
        },
        ConditionExpression="attribute_not_exists(key_hash)",
    )

    return response(200, {
        "server_id": server_id,
        "name": name,
        "key": key,
        "ingest_url": INGEST_URL,
        "install_url": INSTALL_URL,
        "command": f"curl -sSL {INSTALL_URL} | sudo bash -s -- --key {key} --name {name}",
    })
