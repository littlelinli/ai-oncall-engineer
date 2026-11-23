"""Webhook Telegram-бота Kezekshi: POST /telegram/{secret}.

Telegram присылает сюда каждое сообщение боту и нажатия на кнопки. Подлинность проверяется
заголовком X-Telegram-Bot-Api-Secret-Token (задаётся при setWebhook, см. terraform/accounts.tf).

Команды (их же дублируют кнопки внизу чата и меню бота):
  /start <код>  привязать этот чат к аккаунту (код выдаёт сайт: POST /me/telegram)
  /servers      мои серверы и их состояние
  /lang         язык уведомлений (кнопки) или /lang ru|en|both
  /stop         отвязать чат (с подтверждением)
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
SITE = os.environ.get("SITE_URL", "").rstrip("/")

TEXT = {
    "help": {
        "ru": "Kezekshi следит за вашими серверами и пишет сюда, когда что-то ломается или скоро сломается.\n\n"
              "Кнопки внизу: мои серверы, язык уведомлений, отключить уведомления.",
        "en": "Kezekshi watches your servers and writes here when something breaks or is about to break.\n\n"
              "Buttons below: my servers, notification language, turn notifications off.",
    },
    "linked": {"ru": "Готово! Уведомления по вашим серверам ({email}) будут приходить сюда.",
               "en": "Done! Notifications for your servers ({email}) will come here."},
    "bad_code": {"ru": "Ссылка устарела или уже использована. Получите новую на сайте: Дашборд → Подключить Telegram.",
                 "en": "The link has expired or was already used. Get a new one on the website: Dashboard → Connect Telegram."},
    "not_linked": {"ru": "Этот чат не привязан к аккаунту. Откройте сайт: Дашборд → Подключить Telegram.",
                   "en": "This chat is not linked to an account. Open the website: Dashboard → Connect Telegram."},
    "stop_ask": {"ru": "Отключить уведомления в этом чате?", "en": "Turn notifications off in this chat?"},
    "stopped": {"ru": "Уведомления отключены. Привязать снова можно на сайте.",
                "en": "Notifications are off. You can link the chat again on the website."},
    "kept": {"ru": "Хорошо, уведомления остаются.", "en": "OK, notifications stay on."},
    "no_servers": {"ru": "Серверов пока нет. Подключите первый на сайте: Подключить сервер.",
                   "en": "No servers yet. Connect the first one on the website: Connect a server."},
    "servers_head": {"ru": "Мои серверы:", "en": "My servers:"},
    "lang_ask": {"ru": "На каком языке присылать уведомления?", "en": "Which language should notifications use?"},
    "lang_set": {"ru": "Язык уведомлений: {name}", "en": "Notification language: {name}"},
}
LANG_NAMES = {"ru": "Русский", "en": "English", "both": "RU + EN"}
STATUS = {"online": "🟢", "pending": "⏳", "offline": "🔴"}

# Кнопки внизу чата: подпись -> команда. Подписи на обоих языках, чтобы понимать любые.
BUTTONS = {
    "servers": {"ru": "📋 Мои серверы", "en": "📋 My servers"},
    "lang": {"ru": "🌐 Язык", "en": "🌐 Language"},
    "help": {"ru": "❓ Помощь", "en": "❓ Help"},
    "stop": {"ru": "🔕 Отключить", "en": "🔕 Turn off"},
}
BUTTON_TO_CMD = {label: "/" + cmd for cmd, labels in BUTTONS.items() for label in labels.values()}
INLINE = {
    "refresh": {"ru": "🔄 Обновить", "en": "🔄 Refresh"},
    "site": {"ru": "🌐 Открыть сайт", "en": "🌐 Open website"},
    "yes_stop": {"ru": "Да, отключить", "en": "Yes, turn off"},
    "no_stop": {"ru": "Нет", "en": "No"},
}


def one(lang):
    """Для подписей кнопок один язык: both -> ru."""
    return lang if lang in ("ru", "en") else "ru"


def t(key, lang, **kw):
    langs = ["ru", "en"] if lang == "both" else [one(lang)]
    return "\n\n".join(TEXT[key][x].format(**kw) for x in langs)


def keyboard(lang):
    """Постоянные кнопки внизу чата."""
    b = lambda k: {"text": BUTTONS[k][one(lang)]}
    return {"keyboard": [[b("servers"), b("lang")], [b("help"), b("stop")]],
            "resize_keyboard": True, "is_persistent": True}


def tg(method, **body):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def send(chat_id, text, markup=None):
    body = {"chat_id": chat_id, "text": text}
    if markup:
        body["reply_markup"] = markup
    tg("sendMessage", **body)


def edit(chat_id, message_id, text, markup=None):
    body = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if markup:
        body["reply_markup"] = markup
    try:
        tg("editMessageText", **body)
    except Exception as e:  # «message is not modified», если ничего не поменялось
        print(f"edit: {e!r}")


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
    lang = user.get("lang", "both")
    items, kwargs = [], {"FilterExpression": Attr("owner").eq(user["user_id"])}
    while True:
        page = SERVERS.scan(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    if not items:
        return t("no_servers", lang)
    lines = [TEXT["servers_head"][one(lang)]]
    for s in sorted(items, key=lambda x: x.get("name", "")):
        seen = s.get("last_seen", "")
        status = s.get("status", "pending")
        if seen and status == "online":
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(seen)).total_seconds()
            if age > 300:
                status = "offline"
        lines.append(f"{STATUS.get(status, '⚪')} {s.get('name')}"
                     + (f" · {seen[11:16]} UTC" if seen else ""))
    return "\n".join(lines)


def servers_markup(lang):
    row = [{"text": INLINE["refresh"][one(lang)], "callback_data": "servers"}]
    if SITE:
        row.append({"text": INLINE["site"][one(lang)], "url": f"{SITE}/dashboard.html"})
    return {"inline_keyboard": [row]}


def lang_markup():
    return {"inline_keyboard": [[{"text": name, "callback_data": f"lang:{code}"}
                                 for code, name in LANG_NAMES.items()]]}


def set_lang(user, lang):
    USERS.update_item(Key={"user_id": user["user_id"]}, UpdateExpression="SET lang = :l",
                      ExpressionAttributeValues={":l": lang})


def stop(user):
    USERS.update_item(Key={"user_id": user["user_id"]}, UpdateExpression="REMOVE telegram_chat_id")


def handle(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    text = BUTTON_TO_CMD.get(text, text)
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()

    if cmd == "/start" and arg:
        link = USERS.get_item(Key={"user_id": f"link#{arg}"}).get("Item")
        if not link or int(link.get("ttl", 0)) < datetime.now(timezone.utc).timestamp():
            return send(chat_id, t("bad_code", "both"))
        USERS.delete_item(Key={"user_id": link["user_id"]})
        user = USERS.update_item(
            Key={"user_id": link["owner"]}, UpdateExpression="SET telegram_chat_id = :c, lang = if_not_exists(lang, :l)",
            ExpressionAttributeValues={":c": chat_id, ":l": "both"}, ReturnValues="ALL_NEW")["Attributes"]
        lang = user.get("lang", "both")
        return send(chat_id, t("linked", lang, email=user.get("email", "")) + "\n\n" + t("help", lang),
                    keyboard(lang))

    user = user_by_chat(chat_id)
    if not user:
        return send(chat_id, t("not_linked", "both"), {"remove_keyboard": True})
    ulang = user.get("lang", "both")
    if cmd == "/servers":
        return send(chat_id, servers_text(user), servers_markup(ulang))
    if cmd == "/lang":
        if arg in LANG_NAMES:
            set_lang(user, arg)
            return send(chat_id, t("lang_set", arg, name=LANG_NAMES[arg]), keyboard(arg))
        return send(chat_id, t("lang_ask", ulang), lang_markup())
    if cmd == "/stop":
        return send(chat_id, t("stop_ask", ulang), {"inline_keyboard": [[
            {"text": INLINE["yes_stop"][one(ulang)], "callback_data": "stop:yes"},
            {"text": INLINE["no_stop"][one(ulang)], "callback_data": "stop:no"}]]})
    return send(chat_id, t("help", ulang), keyboard(ulang))


def handle_callback(cb):
    """Нажатие на кнопку под сообщением."""
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    try:
        tg("answerCallbackQuery", callback_query_id=cb["id"])  # убирает «часики» на кнопке
    except Exception as e:
        print(f"answerCallbackQuery: {e!r}")
    if not chat_id:
        return
    user = user_by_chat(chat_id)
    if not user:
        return send(chat_id, t("not_linked", "both"))
    ulang = user.get("lang", "both")
    mid = msg.get("message_id")

    if data == "servers":
        return edit(chat_id, mid, servers_text(user), servers_markup(ulang))
    if data.startswith("lang:") and data[5:] in LANG_NAMES:
        lang = data[5:]
        set_lang(user, lang)
        edit(chat_id, mid, t("lang_ask", ulang))  # убрать кнопки выбора
        return send(chat_id, t("lang_set", lang, name=LANG_NAMES[lang]), keyboard(lang))  # и обновить кнопки внизу
    if data == "stop:yes":
        stop(user)
        edit(chat_id, mid, t("stop_ask", ulang))
        return send(chat_id, t("stopped", ulang), {"remove_keyboard": True})
    if data == "stop:no":
        return edit(chat_id, mid, t("kept", ulang))


def lambda_handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not SECRET or headers.get("x-telegram-bot-api-secret-token") != SECRET:
        return {"statusCode": 403, "body": "forbidden"}
    try:
        update = json.loads(event.get("body") or "{}")
        if update.get("callback_query"):
            handle_callback(update["callback_query"])
        else:
            msg = update.get("message") or update.get("edited_message")
            if msg and "chat" in msg:
                handle(msg)
    except Exception as e:  # ошибку пишем в лог, но Telegram отвечаем 200
        print(f"ERROR: {e!r}")
    return {"statusCode": 200, "body": "ok"}
