"""API личного кабинета Kezekshi. Все маршруты требуют вход (JWT от Amazon Cognito,
его проверяет API Gateway). Пользователь = claim "sub", почта = claim "email".

  GET    /me                          профиль: почта, язык, подключён ли Telegram, сколько серверов
  PATCH  /me                          {"lang": "ru" | "en" | "both", "email_alerts": true | false}
                                      почта: при включении AWS присылает письмо «Confirm subscription»
  GET    /me/servers                  мои серверы + последний прогноз ML-агента
  DELETE /me/servers/{id}             отключить сервер (запись и его алармы удаляются)
  GET    /me/servers/{id}/metrics     ряды метрик и прогноза за ?hours=6 (для графиков)
  GET    /me/incidents                инциденты моих серверов, новые сверху
  POST   /me/telegram                 ссылка на бота для привязки Telegram (действует 15 минут)
  DELETE /me/telegram                 отвязать Telegram
"""
import json
import os
import secrets
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

DDB = boto3.resource("dynamodb")
CW = boto3.client("cloudwatch")
SNS = boto3.client("sns")
USER_ALERTS_TOPIC = os.environ.get("USER_ALERTS_TOPIC_ARN", "")
SERVERS = DDB.Table(os.environ["SERVERS_TABLE"])
USERS = DDB.Table(os.environ["USERS_TABLE"])
INCIDENTS = DDB.Table(os.environ["INCIDENTS_TABLE"])
PREDICTIONS = DDB.Table(os.environ["PREDICTIONS_TABLE"])
NAMESPACE = os.environ.get("METRICS_NAMESPACE", "Kezekshi")
ALARM_PREFIX = os.environ.get("ALARM_PREFIX", "ai-oncall-engineer-")
LINK_TTL = 15 * 60
LANGS = ("ru", "en", "both")

_bot_username = None


# ---------------- общее ----------------

def _json_default(v):
    if isinstance(v, Decimal):
        return int(v) if v % 1 == 0 else float(v)
    if isinstance(v, set):
        return sorted(v)
    raise TypeError(type(v))


def response(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps(body, ensure_ascii=False, default=_json_default)}


def claims(event):
    return event["requestContext"]["authorizer"]["jwt"]["claims"]


def scan_all(table, **kwargs):
    items = []
    while True:
        page = table.scan(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def my_servers(user_id):
    return scan_all(SERVERS, FilterExpression=Attr("owner").eq(user_id))


def find_server(user_id, server_id):
    for s in my_servers(user_id):
        if s.get("server_id") == server_id:
            return s
    return None


def public_server(s):
    """Без key_hash: хэш ключа агента наружу не отдаём."""
    keep = ("server_id", "name", "status", "created_at", "last_seen", "alarm_set", "cloud")
    return {k: s[k] for k in keep if k in s}


def bot_username():
    global _bot_username
    if _bot_username is None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=5) as r:
            _bot_username = json.loads(r.read())["result"]["username"]
    return _bot_username


# ---------------- маршруты ----------------

def email_state(user):
    """off | pending (ждём, пока подтвердит письмо) | on."""
    arn = user.get("email_sub_arn")
    if not user.get("email_alerts") or not arn:
        return "off"
    try:
        attrs = SNS.get_subscription_attributes(SubscriptionArn=arn)["Attributes"]
        return "pending" if attrs.get("PendingConfirmation") == "true" else "on"
    except Exception as e:  # подписку удалили или не подтвердили за 3 дня
        print(f"subscription {arn}: {e!r}")
        return "off"


def get_me(user_id, email):
    user = USERS.get_item(Key={"user_id": user_id}).get("Item") or {}
    if not user:
        user = {"user_id": user_id, "email": email, "lang": "both",
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        USERS.put_item(Item=user)
    state = email_state(user)
    return response(200, {
        "email": user.get("email", email),
        "lang": user.get("lang", "both"),
        "telegram_linked": bool(user.get("telegram_chat_id")),
        "email_alerts": state != "off",
        "email_status": state,
        "servers": len(my_servers(user_id)),
    })


def set_email_alerts(user_id, email, on):
    user = USERS.get_item(Key={"user_id": user_id}).get("Item") or {}
    arn = user.get("email_sub_arn")
    if on:
        if not USER_ALERTS_TOPIC:
            raise RuntimeError("USER_ALERTS_TOPIC_ARN не задан")
        if not (user.get("email_alerts") and arn and email_state(user) != "off"):
            arn = SNS.subscribe(
                TopicArn=USER_ALERTS_TOPIC, Protocol="email", Endpoint=email,
                Attributes={"FilterPolicy": json.dumps({"owner": [user_id]})},
                ReturnSubscriptionArn=True)["SubscriptionArn"]
        USERS.update_item(Key={"user_id": user_id}, UpdateExpression="SET email_alerts = :t, email_sub_arn = :a, email = :e",
                          ExpressionAttributeValues={":t": True, ":a": arn, ":e": email})
    else:
        if arn:
            try:
                SNS.unsubscribe(SubscriptionArn=arn)
            except Exception as e:
                print(f"unsubscribe {arn}: {e!r}")
        USERS.update_item(Key={"user_id": user_id}, UpdateExpression="SET email_alerts = :f REMOVE email_sub_arn",
                          ExpressionAttributeValues={":f": False})


def patch_me(user_id, email, body):
    if "lang" not in body and "email_alerts" not in body:
        return response(400, {"error": "lang или email_alerts"})
    if "lang" in body:
        if body["lang"] not in LANGS:
            return response(400, {"error": "lang: ru, en или both"})
        USERS.update_item(Key={"user_id": user_id}, UpdateExpression="SET lang = :l, email = :e",
                          ExpressionAttributeValues={":l": body["lang"], ":e": email})
    if "email_alerts" in body:
        if not isinstance(body["email_alerts"], bool):
            return response(400, {"error": "email_alerts: true или false"})
        set_email_alerts(user_id, email, body["email_alerts"])
    return get_me(user_id, email)


def latest_forecast(server_id):
    # ts прогнозов это ISO-время ("2026-..."), записи предупреждений начинаются с "warn#"
    items = PREDICTIONS.query(KeyConditionExpression=Key("server_id").eq(server_id) & Key("ts").begins_with("20"),
                              ScanIndexForward=False, Limit=1).get("Items", [])
    if not items:
        return None
    p = items[0]
    return {"ts": p["ts"], "forecast": p.get("forecast", {}), "risks": p.get("risks", [])}


def get_servers(user_id):
    out = []
    for s in sorted(my_servers(user_id), key=lambda x: x.get("created_at", "")):
        item = public_server(s)
        try:
            item["prediction"] = latest_forecast(s["server_id"])
        except Exception as e:
            print(f"forecast {s['server_id']}: {e}")
            item["prediction"] = None
        out.append(item)
    return response(200, out)


def delete_server(user_id, server_id):
    s = find_server(user_id, server_id)
    if not s:
        return response(404, {"error": "server not found"})
    SERVERS.delete_item(Key={"key_hash": s["key_hash"]})
    names = [f"{ALARM_PREFIX}srv-{server_id}-{a}" for a in (s.get("alarm_set") or [])]
    names += [f"{ALARM_PREFIX}srv-{server_id}-{a}" for a in ("cpu-high", "disk-high", "app-errors", "server-down")]
    names = sorted(set(names))
    for i in range(0, len(names), 100):
        CW.delete_alarms(AlarmNames=names[i:i + 100])
    return response(200, {"deleted": server_id})


METRICS = {  # имя в ответе -> (namespace, метрика, статистика)
    "cpu": (None, "CpuPercent", "Average"),
    "mem": (None, "MemPercent", "Average"),
    "disk": (None, "DiskPercent", "Maximum"),
    "errors": (None, "ErrorCount", "Sum"),
    "latency": (None, "LatencyP95", "Average"),
    "cpu_forecast": ("/Forecast", "CpuPercentF30", "Average"),
    "mem_forecast": ("/Forecast", "MemPercentF30", "Average"),
    "disk_forecast": ("/Forecast", "DiskPercentF30", "Average"),
}


def get_metrics(user_id, server_id, hours):
    if not find_server(user_id, server_id):
        return response(404, {"error": "server not found"})
    hours = max(1, min(int(hours or 6), 72))
    period = 60 if hours <= 6 else 300
    end = datetime.now(timezone.utc) + timedelta(minutes=35)  # прогноз смотрит на 30 минут вперёд
    start = end - timedelta(hours=hours, minutes=35)
    dims = [{"Name": "ServerId", "Value": server_id}]
    queries = [{"Id": k, "ReturnData": True, "MetricStat": {
        "Metric": {"Namespace": NAMESPACE + (ns or ""), "MetricName": m, "Dimensions": dims},
        "Period": period, "Stat": st}} for k, (ns, m, st) in METRICS.items()]
    series = {k: [] for k in METRICS}
    token = None
    while True:
        args = {"MetricDataQueries": queries, "StartTime": start, "EndTime": end, "ScanBy": "TimestampAscending"}
        if token:
            args["NextToken"] = token
        resp = CW.get_metric_data(**args)
        for r in resp["MetricDataResults"]:
            series[r["Id"]] += [[t.isoformat(timespec="minutes"), round(v, 2)]
                                for t, v in zip(r["Timestamps"], r["Values"])]
        token = resp.get("NextToken")
        if not token:
            break
    return response(200, {"server_id": server_id, "period": period, "series": series})


def get_incidents(user_id):
    ids = {s["server_id"] for s in my_servers(user_id)}
    if not ids:
        return response(200, [])
    items = [i for i in scan_all(INCIDENTS) if i.get("server_id") in ids]
    items.sort(key=lambda i: i.get("timestamp", ""), reverse=True)
    for i in items:
        i.pop("instance_id", None)
    return response(200, items)


def link_telegram(user_id, email):
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return response(503, {"error": "Telegram bot is not configured"})
    code = secrets.token_urlsafe(12)
    USERS.put_item(Item={"user_id": f"link#{code}", "owner": user_id, "ttl": int(time.time()) + LINK_TTL})
    USERS.update_item(Key={"user_id": user_id}, UpdateExpression="SET email = :e, lang = if_not_exists(lang, :l)",
                      ExpressionAttributeValues={":e": email, ":l": "both"})
    name = bot_username()
    return response(200, {"url": f"https://t.me/{name}?start={code}", "bot": name, "expires_in": LINK_TTL})


def unlink_telegram(user_id):
    USERS.update_item(Key={"user_id": user_id}, UpdateExpression="REMOVE telegram_chat_id")
    return response(200, {"telegram_linked": False})


def lambda_handler(event, context):
    try:
        c = claims(event)
    except (KeyError, TypeError):
        return response(401, {"error": "login required"})
    user_id, email = c["sub"], c.get("email", "")
    route = event.get("routeKey", "")
    params = event.get("pathParameters") or {}
    query = event.get("queryStringParameters") or {}
    try:
        body = json.loads(event.get("body") or "{}")
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        return response(400, {"error": "body must be a JSON object"})

    try:
        if route == "GET /me":
            return get_me(user_id, email)
        if route == "PATCH /me":
            return patch_me(user_id, email, body)
        if route == "GET /me/servers":
            return get_servers(user_id)
        if route == "DELETE /me/servers/{id}":
            return delete_server(user_id, params.get("id", ""))
        if route == "GET /me/servers/{id}/metrics":
            return get_metrics(user_id, params.get("id", ""), query.get("hours"))
        if route == "GET /me/incidents":
            return get_incidents(user_id)
        if route == "POST /me/telegram":
            return link_telegram(user_id, email)
        if route == "DELETE /me/telegram":
            return unlink_telegram(user_id)
    except Exception as e:
        print(f"ERROR {route}: {e!r}")
        return response(500, {"error": "internal error"})
    return response(404, {"error": "unknown route"})
