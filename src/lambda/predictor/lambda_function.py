"""ML-агент Kezekshi: прогноз проблем до того, как сработает аларм.

Запускается EventBridge раз в 5 минут. Для каждого сервера, который шлёт данные агентом:
1. Берёт из CloudWatch (namespace METRICS_NAMESPACE, измерение ServerId) CPU, память,
   диск и ошибки: поминутно за 3 часа и по 5 минут за HISTORY_DAYS дней.
2. Прогоняет через model.forecast(): прогноз на 15, 30 и 60 минут и тренд.
3. Проверяет риски (правила ниже), при риске шлёт предупреждение в Telegram,
   не чаще раза в WARN_COOLDOWN секунд на один риск одного сервера.
4. Сохраняет прогноз в DynamoDB (PREDICTIONS_TABLE), хранится TTL_DAYS дней.
5. Пишет прогноз на 30 минут в CloudWatch (FORECAST_NAMESPACE) с отметкой времени
   «на когда прогноз». На одном графике видно факт и прогноз, удобно для диплома.

Ручной запуск для проверки: тестовое событие {"dry_run": true} ничего не шлёт и не пишет,
только возвращает прогнозы.
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

import boto3

import model

NAMESPACE = os.environ.get("METRICS_NAMESPACE", "Kezekshi")
FORECAST_NS = os.environ.get("FORECAST_NAMESPACE", "Kezekshi/Forecast")
PREDICTIONS_TABLE = os.environ.get("PREDICTIONS_TABLE", "kezekshi-predictions")
SERVERS_TABLE = os.environ.get("SERVERS_TABLE", "incident-demo-servers")
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "7"))
WARN_COOLDOWN = int(os.environ.get("WARN_COOLDOWN", "3600"))
TTL_DAYS = int(os.environ.get("TTL_DAYS", "30"))

CW = boto3.client("cloudwatch")
DDB = boto3.resource("dynamodb")

# наше имя -> (метрика агента, статистика)
METRICS = {
    "cpu": ("CpuPercent", "Average"),
    "mem": ("MemPercent", "Average"),
    "disk": ("DiskPercent", "Maximum"),
    "errors": ("ErrorCount", "Sum"),
}


# ---------------- данные ----------------

def list_servers():
    """ServerId всех серверов, от которых были данные за последние 3 часа."""
    ids = set()
    for page in CW.get_paginator("list_metrics").paginate(
            Namespace=NAMESPACE, MetricName="Heartbeat", RecentlyActive="PT3H"):
        for m in page["Metrics"]:
            for d in m["Dimensions"]:
                if d["Name"] == "ServerId":
                    ids.add(d["Value"])
    return sorted(ids)


def server_names():
    try:
        items = DDB.Table(SERVERS_TABLE).scan(ProjectionExpression="server_id, #n",
                                              ExpressionAttributeNames={"#n": "name"})["Items"]
        return {i["server_id"]: i.get("name", i["server_id"]) for i in items if "server_id" in i}
    except Exception as e:  # имена не обязательны
        print(f"server names unavailable: {e}")
        return {}


def fetch_series(server_id, now_ts):
    end = datetime.fromtimestamp(now_ts, timezone.utc)
    dims = [{"Name": "ServerId", "Value": server_id}]
    queries = []
    for key, (metric, stat) in METRICS.items():
        for kind, period in (("recent", 60), ("history", 300)):
            queries.append({
                "Id": f"{kind}_{key}",
                "MetricStat": {"Metric": {"Namespace": NAMESPACE, "MetricName": metric, "Dimensions": dims},
                               "Period": period, "Stat": stat},
                "ReturnData": True,
            })
    series = {k: {"recent": [], "history": []} for k in METRICS}
    starts = {"recent": now_ts - 3 * 3600, "history": now_ts - HISTORY_DAYS * 86400}
    # два вызова: у поминутных и пятиминутных запросов разный период начала
    for kind in ("recent", "history"):
        token = None
        while True:
            args = {"MetricDataQueries": [q for q in queries if q["Id"].startswith(kind)],
                    "StartTime": datetime.fromtimestamp(starts[kind], timezone.utc),
                    "EndTime": end, "ScanBy": "TimestampAscending"}
            if token:
                args["NextToken"] = token
            resp = CW.get_metric_data(**args)
            for r in resp["MetricDataResults"]:
                key = r["Id"].split("_", 1)[1]
                series[key][kind].extend((t.timestamp(), v) for t, v in zip(r["Timestamps"], r["Values"]))
            token = resp.get("NextToken")
            if not token:
                break
    for s in series.values():
        for kind in s:
            s[kind].sort()
    return series


# ---------------- риски ----------------

def find_risks(fc):
    """Правила поверх прогноза. Возвращает [(код, текст)]."""
    risks = []
    cpu = fc.get("cpu")
    if cpu and cpu["now"] < 80:
        for h in model.HORIZONS:
            if cpu[f"f{h}"] > 80:
                risks.append(("cpu_overload",
                              f"CPU может превысить 80% примерно через {h} мин "
                              f"(сейчас {cpu['now']:.0f}%, прогноз {cpu[f'f{h}']:.0f}%)"))
                break
    mem = fc.get("mem")
    if mem and mem["now"] > 50 and mem["trend_per_hour"] > 0.5:
        eta = (95 - mem["now"]) / mem["trend_per_hour"]
        if 0 < eta < 3:
            risks.append(("memory_leak",
                          f"Память растёт на {mem['trend_per_hour']:.1f}% в час, похоже на утечку. "
                          f"Закончится примерно через {eta * 60:.0f} мин (сейчас {mem['now']:.0f}%)"))
    disk = fc.get("disk")
    if disk and disk["now"] > 60 and disk["trend_per_hour"] > 0.05:
        eta = (95 - disk["now"]) / disk["trend_per_hour"]
        if 0 < eta < 24:
            risks.append(("disk_full",
                          f"Диск заполняется на {disk['trend_per_hour']:.2f}% в час, "
                          f"заполнится примерно через {eta:.1f} ч (сейчас {disk['now']:.0f}%)"))
    err = fc.get("errors")
    if err and err["now"] <= 5 and err["f30"] > 5:
        risks.append(("errors_rising",
                      f"Ошибок становится больше: прогноз {err['f30']:.0f} в минуту через 30 мин "
                      f"(сейчас {err['now']:.0f})"))
    return risks


# ---------------- вывод ----------------

def to_ddb(x):
    return json.loads(json.dumps(x), parse_float=Decimal)


def should_warn(table, server_id, code, now_ts, text=""):
    key = {"server_id": server_id, "ts": f"warn#{code}"}
    last = table.get_item(Key=key).get("Item")
    if last and now_ts - float(last.get("sent_at", 0)) < WARN_COOLDOWN:
        return False
    # text и sent_at читает incident-analyzer: «ML-агент предупреждал за N мин»
    table.put_item(Item={**key, "sent_at": Decimal(str(int(now_ts))), "text": text,
                         "expires_at": int(now_ts) + TTL_DAYS * 86400})
    return True


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram не настроен, пропускаю")
        return
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def put_forecast_metrics(server_id, fc, now_ts):
    when = datetime.fromtimestamp(now_ts + 30 * 60, timezone.utc)  # на когда прогноз
    dims = [{"Name": "ServerId", "Value": server_id}]
    data = [{"MetricName": f"{METRICS[k][0]}F30", "Dimensions": dims, "Timestamp": when,
             "Value": float(v["f30"])} for k, v in fc.items()]
    if data:
        CW.put_metric_data(Namespace=FORECAST_NS, MetricData=data)


def handler(event, context):
    dry = bool((event or {}).get("dry_run"))
    now_ts = time.time() // 60 * 60
    table = DDB.Table(PREDICTIONS_TABLE)
    names = server_names()
    result = {}
    for sid in list_servers():
        try:
            fc = model.forecast(fetch_series(sid, now_ts), now_ts)
            if not fc:
                continue
            risks = find_risks(fc)
            result[sid] = {"forecast": fc, "risks": [c for c, _ in risks]}
            if dry:
                continue
            table.put_item(Item={
                "server_id": sid,
                "ts": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(timespec="minutes"),
                "model": model.MODEL_VERSION,
                "forecast": to_ddb(fc),
                "risks": [{"code": c, "text": t} for c, t in risks],
                "expires_at": int(now_ts) + TTL_DAYS * 86400,
            })
            put_forecast_metrics(sid, fc, now_ts)
            for code, text in risks:
                if should_warn(table, sid, code, now_ts, text):
                    send_telegram(f"🔮 Прогноз: {names.get(sid, sid)}\n{text}\n"
                                  f"Аларм ещё не сработал, это предупреждение заранее.")
        except Exception as e:  # один сломанный сервер не должен мешать остальным
            print(f"server {sid}: {e!r}")
    print(json.dumps({"servers": len(result), "risks": {k: v["risks"] for k, v in result.items()}}))
    return result
