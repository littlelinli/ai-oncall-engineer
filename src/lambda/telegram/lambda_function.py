"""Webhook Telegram-бота Kezekshi: POST /telegram/{secret}.

Telegram присылает сюда каждое сообщение боту. Подлинность проверяется заголовком
X-Telegram-Bot-Api-Secret-Token (задаётся при setWebhook, см. terraform/accounts.tf).

Команды:
  /start <код>  привязать этот чат к аккаунту (код выдаёт сайт: POST /me/telegram)
  /servers      мои серверы и их состояние
  /lang ru|en|both  язык уведомлений
  /stop         отвязать чат
  /help         список команд
Отвечаем всегда 200, иначе Telegram будет повторять сообщение.
"""
import json
import os
import urllib.request
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr

DDB = boto3.resource("dynamodb")
USERS = DDB.Table(os.environ["USERS_TABLE"])
SERVERS = DDB.Table(os.environ["SERVERS_TABLE"])
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SECRET = os.environ.get("WEBHOOK_SECRET", "")

TEXT = {
    "help": {
        "ru": "Kezekshi следит за вашими серверами и пишет сюда, когда что-то ломается или скоро сломается.\n\n"
              "/servers — мои серверы\n/lang ru|en|both — язык уведомлений\n/stop — отключить уведомления",
        "en": "Kezekshi watches your servers and writes here when something breaks or is about to break.\n\n"
              "/servers — my servers\n/lang ru|en|both — notification language\n/stop — turn notifications off",
    },
    "linked": {"ru": "Готово! Уведомления по вашим серверам ({email}) будут приходить сюда.",
               "en": "Done! Notifications for your servers ({email}) will come here."},
    "bad_code": {"ru": "Ссылка устарела или уже использована. Получите новую на сайте: Дашборд → Подключить Telegram.",
                 "en": "The link has expired or was already used. Get a new one on the website: Dashboard → Connect Telegram."},
    "not_linked": {"ru": "Этот чат не привязан к аккаунту. Откройте сайт: Дашборд → Подключить Telegram.",
                   "en": "This chat is not linked to an account. Open the website: Dashboard → Connect Telegram."},
    "stopped": {"ru": "Уведомления отключены. Привязать снова можно на сайте.",
                "en": "Notifications are off. You can link the chat again on the website."},
    "no_servers": {"ru": "Серверов пока нет. Подключите первый на сайте: Подключить сервер.",
                   "en": "No servers yet. Connect the first one on the website: Connect a server."},
    "lang_set": {"ru": "Язык уведомлений: {lang}", "en": "Notification language: {lang}"},
    "lang_bad": {"ru": "Пример: /lang ru, /lang en или /lang both",
                 "en": "Example: /lang ru, /lang en or /lang both"},
}
STATUS = {"online": "🟢", "pending": "⏳", "offline": "🔴"}


def t(key, lang, **kw):
    langs = ["ru", "en"] if lang == "both" else [lang if lang in ("ru", "en") else "ru"]
    return "\n\n".join(TEXT[key][x].format(**kw) for x in langs)


def send(chat_id, text):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def user_by_chat(chat_id):
    kwargs = {"FilterExpression": Attr("telegram_chat_id").eq(chat_id)}
    while True:
        page = USERS.scan(**kwargs)
        if page.get("Items"):
            return page["Items"][0]
        if "LastEvaluatedKey" not in page:
            return None
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def servers_text(user):
    items, kwargs = [], {"FilterExpression": Attr("owner").eq(user["user_id"])}
    while True:
        page = SERVERS.scan(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    if not items:
        return t("no_servers", user.get("lang", "both"))
    lines = []
    for s in sorted(items, key=lambda x: x.get("name", "")):
        seen = s.get("last_seen", "")
        status = s.get("status", "pending")
        if seen and status == "online":
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(seen)).total_seconds()
            if age > 300:
                status = "offline"
        lines.append(f"{STATUS.get(status, '⚪')} {s.get('name')} ({s.get('server_id')})"
                     + (f" · {seen[:16].replace('T', ' ')} UTC" if seen else ""))
    return "\n".join(lines)


def handle(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()

    if cmd == "/start" and arg:
        link = USERS.get_item(Key={"user_id": f"link#{arg.strip()}"}).get("Item")
        if not link or int(link.get("ttl", 0)) < datetime.now(timezone.utc).timestamp():
            return send(chat_id, t("bad_code", "both"))
        USERS.delete_item(Key={"user_id": link["user_id"]})
        user = USERS.update_item(
            Key={"user_id": link["owner"]}, UpdateExpression="SET telegram_chat_id = :c, lang = if_not_exists(lang, :l)",
            ExpressionAttributeValues={":c": chat_id, ":l": "both"}, ReturnValues="ALL_NEW")["Attributes"]
        return send(chat_id, t("linked", user.get("lang", "both"), email=user.get("email", "")))

    user = user_by_chat(chat_id)
    if cmd in ("/start", "/help") or not user:
        return send(chat_id, t("help", user.get("lang", "both") if user else "both")
                    + ("" if user else "\n\n" + t("not_linked", "both")))
    ulang = user.get("lang", "both")
    if cmd == "/servers":
        return send(chat_id, servers_text(user))
    if cmd == "/lang":
        if arg.strip() not in ("ru", "en", "both"):
            return send(chat_id, t("lang_bad", ulang))
        USERS.update_item(Key={"user_id": user["user_id"]}, UpdateExpression="SET lang = :l",
                          ExpressionAttributeValues={":l": arg.strip()})
        return send(chat_id, t("lang_set", arg.strip(), lang=arg.strip()))
    if cmd == "/stop":
        USERS.update_item(Key={"user_id": user["user_id"]}, UpdateExpression="REMOVE telegram_chat_id")
        return send(chat_id, t("stopped", ulang))
    return send(chat_id, t("help", ulang))


def lambda_handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not SECRET or headers.get("x-telegram-bot-api-secret-token") != SECRET:
        return {"statusCode": 403, "body": "forbidden"}
    try:
        update = json.loads(event.get("body") or "{}")
        msg = update.get("message") or update.get("edited_message")
        if msg and "chat" in msg:
            handle(msg)
    except Exception as e:  # ошибку пишем в лог, но Telegram отвечаем 200
        print(f"ERROR: {e!r}")
    return {"statusCode": 200, "body": "ok"}
