"""Кому слать уведомление о сервере. Один и тот же файл лежит в
src/lambda/incident_processor и src/lambda/predictor: при правке менять оба.

1. Владельцу сервера (тот, кто подключил его на сайте), если он привязал Telegram.
   Язык берётся из его настроек (ru, en или both).
2. В общий чат команды (TELEGRAM_CHAT_ID), если у сервера нет владельца
   (например, демо-сервер) или если NOTIFY_TEAM_ALL=true.
"""
import os

import boto3
from boto3.dynamodb.conditions import Attr

_ddb = boto3.resource("dynamodb")


def _langs(value, default):
    if value == "ru":
        return ["ru"]
    if value == "en":
        return ["en"]
    if value == "both":
        return ["ru", "en"]
    return default


def owner_of(server_id):
    table = os.environ.get("SERVERS_TABLE")
    if not table or not server_id:
        return None
    kwargs = {"FilterExpression": Attr("server_id").eq(server_id), "ProjectionExpression": "#o",
              "ExpressionAttributeNames": {"#o": "owner"}}
    t = _ddb.Table(table)
    while True:
        page = t.scan(**kwargs)
        for item in page.get("Items", []):
            if item.get("owner"):
                return item["owner"]
        if "LastEvaluatedKey" not in page:
            return None
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def owner_profile(server_id):
    """(owner, запись пользователя) или (None, {})."""
    owner = owner_of(server_id)
    if owner and os.environ.get("USERS_TABLE"):
        return owner, _ddb.Table(os.environ["USERS_TABLE"]).get_item(Key={"user_id": owner}).get("Item") or {}
    return owner, {}


def langs_of(user, default_langs):
    return _langs((user or {}).get("lang"), default_langs)


def telegram_targets(server_id, default_langs):
    """[(chat_id, [языки])] без повторов."""
    out = []
    owner = None
    try:
        owner, user = owner_profile(server_id)
        if user.get("telegram_chat_id"):
            out.append((int(user["telegram_chat_id"]), _langs(user.get("lang"), default_langs)))
    except Exception as e:  # без владельца всё равно уведомим команду
        print(f"recipients: {e!r}")
    team = os.environ.get("TELEGRAM_CHAT_ID")
    if team and (not owner or os.environ.get("NOTIFY_TEAM_ALL", "false").lower() == "true"):
        if all(str(c) != str(team) for c, _ in out):
            out.append((team, default_langs))
    return out
