"""API-функция для сайта: читает инциденты из DynamoDB.

Маршруты (Lambda Function URL, HTTP API или REST с proxy-интеграцией):
  GET /incidents        -> список инцидентов, новые сверху
  GET /incidents/{id}   -> один инцидент

Переменные окружения:
  TABLE_NAME      имя таблицы DynamoDB (по умолчанию aiops-incidents)
  ALLOWED_ORIGIN  домен сайта для CORS (по умолчанию *)
  KEY_NAME        имя ключа партиции таблицы (по умолчанию incident_id)

Нужные права IAM: dynamodb:Scan и dynamodb:GetItem на эту таблицу.
"""
import json
import os
from decimal import Decimal

import boto3

TABLE_NAME = os.environ.get("TABLE_NAME", "aiops-incidents")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
KEY_NAME = os.environ.get("KEY_NAME", "incident_id")  # ключ партиции таблицы: incident_id или id
table = boto3.resource("dynamodb").Table(TABLE_NAME)


def _json_default(value):
    # DynamoDB возвращает числа как Decimal, а json их не понимает
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Cannot serialize {type(value)}")


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
            "Access-Control-Allow-Methods": "GET,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(body, ensure_ascii=False, default=_json_default),
    }


def _list_incidents():
    items, kwargs = [], {}
    while True:  # таблица маленькая, Scan с постраничным чтением достаточно
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    items.sort(key=lambda i: i.get("started_at") or i.get("timestamp") or "", reverse=True)
    return items


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod", "GET")
    if method == "OPTIONS":
        return _response(200, {})
    if method != "GET":
        return _response(405, {"error": "method not allowed"})

    incident_id = (event.get("pathParameters") or {}).get("id")
    try:
        if incident_id:
            item = table.get_item(Key={KEY_NAME: incident_id}).get("Item")
            if not item:
                return _response(404, {"error": "not found"})
            return _response(200, item)
        return _response(200, _list_incidents())
    except Exception as exc:  # отдаём понятную ошибку вместо пустого 502
        print(f"ERROR reading {TABLE_NAME}: {exc}")
        return _response(500, {"error": "could not read incidents"})
