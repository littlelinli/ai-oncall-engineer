"""Датасет для дообучения языковой модели Kezekshi (kezekshi-llm).

Задача модели: по фактам об инциденте и черновику из правил написать «Вероятную причину»
живым языком, 1–3 предложения, без чисел, которых нет в фактах, без советов.

Пример = чат из трёх сообщений (system, user, assistant), как в src/llm/handler.py:
  user      = факты (JSON) + черновик анализатора
  assistant = эталонный текст причины

Факты генерируются из сценариев, которые реально бывают в системе (ShopSim + 9 алармов):
цифры, сервер, сообщения в логах каждый раз разные. Эталонные ответы собираются из
нескольких формулировок на причину, написанных командой. Позже сюда добавляются
настоящие инциденты из DynamoDB с ответами, поправленными вручную (--extra файл JSONL).

    python3 ml/llm/make_dataset.py --n 900 --out ml/llm/data
    -> train.jsonl, val.jsonl, test.jsonl (80 / 10 / 10)
"""
import argparse
import json
import os
import random

SYSTEM = {
    "ru": ("Ты дежурный инженер. По фактам об инциденте на сервере объясни вероятную причину "
           "простым языком, 2–3 коротких предложения, по-русски. Используй только факты ниже. "
           "Не придумывай чисел, сервисов и событий, которых нет в фактах. Не пиши шаги и советы, "
           "только причину. Без приветствий, без списков, без markdown."),
    "en": ("You are an on-call engineer. From the facts about an incident on a server, explain the likely "
           "cause in plain English, 2–3 short sentences. Use only the facts below. Do not invent numbers, "
           "services or events that are not in the facts. Do not give steps or advice, only the cause. "
           "No greetings, no lists, no markdown."),
}
USER = {
    "ru": "Факты (JSON):\n{facts}\n\nЧерновик объяснения:\n{draft}\n\nПерепиши черновик лучше, сохранив смысл и числа.",
    "en": "Facts (JSON):\n{facts}\n\nDraft explanation:\n{draft}\n\nRewrite the draft better, keeping its meaning and numbers.",
}

SERVERS = ["demo-shop", "shop-api-prod", "billing-1", "web-01", "auth-service", "catalog-eu", "worker-2",
           "landing", "crm-backend", "media-store"]


def r1(x):
    return round(x, 1)


# Каждый сценарий: alarm, cause, функция, которая делает (значения, логи), черновики и эталоны.
# В текстах {..} подставляются значения; в эталон попадают только числа из фактов.
SCENARIOS = [
    {
        "cause": "traffic_surge", "alarm": "cpu-high",
        "make": lambda r: {"value": r.randint(81, 100), "ratio": r1(r.uniform(1.8, 6.0)), "cpu": None},
        "title": ("Высокая нагрузка на CPU: {value}%", "High CPU load: {value}%"),
        "draft": ("Наплыв посетителей: запросов в {ratio} раза больше обычного, процессор не справляется с потоком.",
                  "Traffic surge: {ratio}x more requests than usual, the CPU cannot keep up."),
        "ru": ["Процессор загружен на {value}%, потому что на сайт пришло в {ratio} раза больше запросов, чем обычно. Сервер не успевает обрабатывать такой поток.",
               "Причина в наплыве посетителей: запросов стало в {ratio} раза больше обычного, и процессору не хватает мощности.",
               "Нагрузку создаёт сам трафик. Запросов в {ratio} раза больше, чем обычно, поэтому CPU держится на {value}%."],
        "en": ["The CPU is at {value}% because the site is getting {ratio} times more requests than usual. The server cannot process that much traffic.",
               "This is a traffic surge: there are {ratio}x more requests than normal, and the CPU does not have enough capacity.",
               "The load comes from the traffic itself. With {ratio}x the usual requests, the CPU stays at {value}%."],
    },
    {
        "cause": "background_process", "alarm": "cpu-high",
        "make": lambda r: {"value": r.randint(81, 100), "ratio": r1(r.uniform(0.8, 1.3))},
        "title": ("Высокая нагрузка на CPU: {value}%", "High CPU load: {value}%"),
        "draft": ("Посетителей обычное количество, значит процессор загружает не сайт, а фоновая задача: бэкап, пересчёт отчётов или зависший скрипт.",
                  "Traffic is normal, so the CPU is used not by the website but by a background job: a backup, a report rebuild or a stuck script."),
        "ru": ["Трафик обычный, поэтому процессор на {value}% загружает не сайт. Скорее всего, работает фоновая задача: бэкап, отчёт или зависший скрипт.",
               "Запросов столько же, сколько всегда, а CPU занят на {value}%. Значит, ресурсы съедает какой-то фоновый процесс, а не посетители.",
               "Нагрузка не связана с посетителями. Процессор занят фоновой задачей, например бэкапом или зависшим скриптом."],
        "en": ["Traffic is normal, so it is not the website that keeps the CPU at {value}%. Most likely a background job is running: a backup, a report or a stuck script.",
               "There are as many requests as usual, yet the CPU is at {value}%. Some background process is using it, not the visitors.",
               "The load is not caused by visitors. A background task, such as a backup or a stuck script, is using the CPU."],
    },
    {
        "cause": "memory_leak", "alarm": "mem-high",
        "make": lambda r: {"value": r.randint(90, 99), "trend": r1(r.uniform(3, 40)), "ratio": r1(r.uniform(0.8, 1.3))},
        "title": ("Заканчивается память: {value}%", "Memory is running out: {value}%"),
        "draft": ("Память растёт на {trend}% в час при обычной нагрузке, это похоже на утечку памяти в приложении.",
                  "Memory grows by {trend}% per hour while traffic is normal, this looks like a memory leak in the application."),
        "ru": ["Память растёт на {trend}% в час, хотя нагрузка обычная. Так обычно выглядит утечка памяти в приложении.",
               "Похоже на утечку памяти: занято уже {value}%, и каждый час добавляется ещё {trend}%, а трафик при этом не вырос.",
               "Приложение не освобождает память: она растёт на {trend}% в час без роста нагрузки. Это признак утечки."],
        "en": ["Memory keeps growing by {trend}% per hour while traffic is normal. That is what a memory leak in the application usually looks like.",
               "This looks like a memory leak: {value}% is already used, and another {trend}% is added every hour without more traffic.",
               "The application is not releasing memory: usage grows by {trend}% per hour with no increase in load. This points to a leak."],
    },
    {
        "cause": "memory_pressure", "alarm": "mem-high",
        "make": lambda r: {"value": r.randint(90, 99), "ratio": r1(r.uniform(0.8, 1.3))},
        "title": ("Заканчивается память: {value}%", "Memory is running out: {value}%"),
        "draft": ("Памяти не хватает процессам на сервере, резкого роста при этом не было.",
                  "Processes on the server do not have enough memory, there was no sudden growth."),
        "ru": ["Процессам на сервере просто не хватает памяти: занято {value}%, при этом резкого скачка не было.",
               "Память занята на {value}% без резкого роста. Серверу не хватает памяти для всех запущенных процессов.",
               "Резкой утечки нет, память заполнялась постепенно. Процессам на сервере тесно."],
        "en": ["The processes on the server simply do not have enough memory: {value}% is used, without any sudden jump.",
               "Memory is at {value}% with no sharp growth. The server does not have enough memory for everything running on it.",
               "There is no sudden leak, memory filled up gradually. The processes on the server are short of memory."],
    },
    {
        "cause": "log_growth", "alarm": "disk-high",
        "make": lambda r: {"value": r.randint(86, 99), "trend": r1(r.uniform(0.2, 3.0)), "ratio": r1(r.uniform(3, 12))},
        "title": ("Заканчивается место на диске: {value}%", "Disk is running out of space: {value}%"),
        "draft": ("Диск забивают логи: приложение пишет их намного больше обычного.",
                  "Logs fill the disk: the application writes far more of them than usual."),
        "ru": ["Место на диске съедают логи: приложение пишет их намного больше обычного, и диск заполнен уже на {value}%.",
               "Диск заполняется логами. Их стало в разы больше, чем обычно.",
               "Причина в логах: приложение пишет слишком много, поэтому диск занят на {value}%."],
        "en": ["Logs are eating the disk space: the application writes far more of them than usual, and the disk is already {value}% full.",
               "The disk is filling up with logs. There are many times more of them than usual.",
               "The cause is logging: the application writes too much, so the disk is {value}% full."],
    },
    {
        "cause": "files_accumulate", "alarm": "disk-high",
        "make": lambda r: {"value": r.randint(86, 99), "trend": r1(r.uniform(0.1, 1.5)), "ratio": r1(r.uniform(0.8, 1.4))},
        "title": ("Заканчивается место на диске: {value}%", "Disk is running out of space: {value}%"),
        "draft": ("Диск постепенно заполняется файлами: загрузки, бэкапы или временные файлы не удаляются.",
                  "The disk slowly fills with files: uploads, backups or temporary files are not removed."),
        "ru": ["Диск постепенно забивается файлами, примерно на {trend}% в час. Похоже, загрузки или временные файлы перестали удаляться.",
               "Место заканчивается медленно, на {trend}% в час. Скорее всего, не работает очистка загрузок, бэкапов или временных файлов.",
               "Файлы копятся и не удаляются, поэтому диск занят уже на {value}%."],
        "en": ["The disk is slowly filling with files, about {trend}% per hour. It looks like uploads or temporary files are no longer being removed.",
               "Space is running out slowly, by {trend}% per hour. The cleanup of uploads, backups or temporary files is probably not working.",
               "Files keep piling up and are not removed, so the disk is already {value}% full."],
    },
    {
        "cause": "disk_jump", "alarm": "disk-high",
        "make": lambda r: {"value": r.randint(86, 99), "ratio": r1(r.uniform(0.8, 1.3))},
        "title": ("Заканчивается место на диске: {value}%", "Disk is running out of space: {value}%"),
        "draft": ("Место на диске резко закончилось: скорее всего, появился один большой файл.",
                  "Disk space ran out suddenly: most likely one large file appeared."),
        "ru": ["Место на диске закончилось резко, а не постепенно. Скорее всего, появился один большой файл.",
               "Диск заполнился скачком до {value}%. Так бывает, когда кто-то создаёт один очень большой файл.",
               "Резкий скачок заполнения диска говорит о большом файле, а не о постепенном накоплении."],
        "en": ["Disk space ran out suddenly, not gradually. Most likely one large file appeared.",
               "The disk jumped to {value}% full. This happens when something creates one very large file.",
               "A sudden jump in disk usage points to one large file rather than slow accumulation."],
    },
    {
        "cause": "db_lock", "alarm": "app-errors",
        "make": lambda r: {"value": r.randint(6, 300), "ratio": r1(r.uniform(1.0, 5.0)), "logs": ["bid failed: database is locked"], "path": "/bid"},
        "title": ("Приложение отвечает ошибками: {value} в минуту", "The application returns errors: {value} per minute"),
        "draft": ("Судя по логам, база данных не успевает обрабатывать одновременные запросы (блокировки).",
                  "According to the logs, the database cannot keep up with concurrent requests (lock contention)."),
        "ru": ["По логам видно, что база данных заблокирована: много запросов пытаются писать одновременно, и часть из них падает с ошибкой.",
               "Ошибки идут из-за блокировок в базе данных. Она не справляется с одновременными записями.",
               "База данных не успевает обрабатывать параллельные запросы, поэтому приложение отвечает ошибками."],
        "en": ["The logs show the database is locked: many requests try to write at the same time, and some of them fail.",
               "The errors come from database locks. The database cannot handle so many concurrent writes.",
               "The database cannot keep up with parallel requests, so the application returns errors."],
    },
    {
        "cause": "upstream_slow", "alarm": "app-errors",
        "make": lambda r: {"value": r.randint(6, 200), "ratio": r1(r.uniform(0.8, 1.3)), "logs": ["payment provider timeout"], "path": "/checkout"},
        "title": ("Приложение отвечает ошибками: {value} в минуту", "The application returns errors: {value} per minute"),
        "draft": ("Судя по логам, внешний сервис (например, платёжный) отвечает слишком медленно или с ошибками.",
                  "According to the logs, an external service (for example, the payment provider) is slow or returns errors."),
        "ru": ["Ошибки связаны с внешним платёжным сервисом: он не отвечает вовремя, и оплата падает по таймауту.",
               "Похоже, тормозит платёжный провайдер. Приложение не дожидается его ответа и возвращает ошибку.",
               "Проблема не в самом сервере: внешний сервис оплаты отвечает слишком медленно."],
        "en": ["The errors are caused by the external payment service: it does not answer in time, and checkout fails with a timeout.",
               "The payment provider seems to be slow. The application does not get its answer in time and returns an error.",
               "The problem is not the server itself: the external payment service responds too slowly."],
    },
    {
        "cause": "dependency_down", "alarm": "app-errors",
        "make": lambda r: {"value": r.randint(6, 400), "ratio": r1(r.uniform(0.8, 1.3)), "logs": ["database connection refused"], "path": "/search"},
        "title": ("Приложение отвечает ошибками: {value} в минуту", "The application returns errors: {value} per minute"),
        "draft": ("Судя по логам, приложение не может подключиться к зависимому сервису (база данных, поиск, кэш или внешний API).",
                  "According to the logs, the application cannot connect to a service it depends on (database, search, cache or external API)."),
        "ru": ["Приложение не может подключиться к сервису, от которого зависит: в логах отказ в подключении к базе данных.",
               "Ошибки идут потому, что недоступна база данных или другой нужный приложению сервис.",
               "По логам соединение с базой данных отклоняется, поэтому часть запросов заканчивается ошибкой."],
        "en": ["The application cannot connect to a service it depends on: the logs show the database connection being refused.",
               "The errors happen because the database, or another service the application needs, is unavailable.",
               "The logs show the database connection is refused, so some requests end with an error."],
    },
    {
        "cause": "overload", "alarm": "app-errors",
        "make": lambda r: {"value": r.randint(6, 500), "ratio": r1(r.uniform(2.5, 10.0)), "logs": ["request timed out in queue, server overloaded"]},
        "title": ("Приложение отвечает ошибками: {value} в минуту", "The application returns errors: {value} per minute"),
        "draft": ("Судя по логам, запросы не успевают обрабатываться и отваливаются по таймауту, сервер перегружен.",
                  "According to the logs, requests are not processed in time and fail with a timeout, the server is overloaded."),
        "ru": ["Сервер перегружен: запросов в {ratio} раза больше обычного, они ждут в очереди и отваливаются по таймауту.",
               "Ошибки из-за перегрузки. Запросы не успевают обработаться и отменяются, пока стоят в очереди.",
               "Приложение не справляется с потоком в {ratio} раза больше обычного, поэтому часть запросов заканчивается ошибкой."],
        "en": ["The server is overloaded: there are {ratio}x more requests than usual, they wait in the queue and time out.",
               "The errors come from overload. Requests are not processed in time and get cancelled while waiting in the queue.",
               "The application cannot handle {ratio} times its usual traffic, so some requests end with an error."],
    },
    {
        "cause": "app_down", "alarm": "app-errors",
        "make": lambda r: {"value": r.randint(100, 2000), "ratio": r1(r.uniform(0.8, 1.3)), "logs": ["service unavailable: application workers crashed, waiting for restart"]},
        "title": ("Приложение отвечает ошибками: {value} в минуту", "The application returns errors: {value} per minute"),
        "draft": ("Приложение упало: ошибкой отвечает всё, включая проверку здоровья. Сервер при этом работает, проблема в самом приложении.",
                  "The application is down: everything, including the health check, returns an error. The server itself is up, the problem is the application."),
        "ru": ["Приложение упало: процессы, которые обрабатывают запросы, остановились, и ошибкой отвечает всё, даже проверка здоровья. Сам сервер работает.",
               "Сервер жив, но приложение на нём не работает. Все запросы, включая проверку здоровья, получают ошибку.",
               "Процессы приложения упали, поэтому сайт на любой запрос отвечает ошибкой."],
        "en": ["The application is down: the processes that handle requests have stopped, and everything returns an error, even the health check. The server itself is up.",
               "The server is alive, but the application on it is not working. Every request, including the health check, gets an error.",
               "The application processes have crashed, so the site answers every request with an error."],
    },
    {
        "cause": "bad_deploy", "alarm": "app-errors",
        "make": lambda r: {"value": r.randint(6, 300), "ratio": r1(r.uniform(0.8, 1.3)), "logs": ["NullPointerException in OrderService.calculateTotal"]},
        "title": ("Приложение отвечает ошибками: {value} в минуту", "The application returns errors: {value} per minute"),
        "draft": ("Ошибки идут по разным адресам без явной внешней причины. Так обычно выглядит неудачное обновление или сломанная настройка.",
                  "Errors come from different endpoints without a clear external cause. This usually means a bad deploy or a broken setting."),
        "ru": ["Внешних причин не видно, а ошибки идут из самого кода приложения. Похоже на неудачное обновление.",
               "Приложение падает с исключением в коде на разных страницах. Так обычно бывает после неудачного деплоя или сломанной настройки.",
               "Нагрузка обычная, зависимые сервисы в порядке, а приложение сыпет ошибками. Скорее всего, виновато последнее обновление."],
        "en": ["There is no external cause, the errors come from the application code itself. This looks like a bad deploy.",
               "The application throws an exception in its code on different pages. That usually happens after a bad deploy or a broken setting.",
               "Traffic is normal and dependencies are fine, yet the application keeps failing. The latest update is the most likely cause."],
    },
    {
        "cause": "cpu_saturation", "alarm": "slow-responses",
        "make": lambda r: {"value": r.randint(2100, 9000), "cpu": r.randint(82, 100), "ratio": r1(r.uniform(0.8, 1.5))},
        "title": ("Сайт отвечает медленно: {sec} с", "The website is slow: {sec} s"),
        "draft": ("Процессор загружен на {cpu}%, запросам не хватает CPU.", "The CPU is at {cpu}%, requests do not get enough CPU time."),
        "ru": ["Сайт тормозит, потому что процессор загружен на {cpu}%. Запросам не хватает процессорного времени.",
               "Ответы медленные из-за перегруженного CPU: он занят на {cpu}%.",
               "Процессор работает на пределе, {cpu}%, и каждый запрос обрабатывается дольше обычного."],
        "en": ["The site is slow because the CPU is at {cpu}%. Requests do not get enough CPU time.",
               "Responses are slow because the CPU is overloaded at {cpu}%.",
               "The CPU is at its limit, {cpu}%, so every request takes longer than usual."],
    },
    {
        "cause": "traffic_surge", "alarm": "slow-responses",
        "make": lambda r: {"value": r.randint(2100, 9000), "cpu": r.randint(20, 70), "ratio": r1(r.uniform(1.8, 8.0))},
        "title": ("Сайт отвечает медленно: {sec} с", "The website is slow: {sec} s"),
        "draft": ("Наплыв посетителей: запросов в {ratio} раза больше обычного. Запросы стоят в очереди, сервер ещё отвечает, но всё медленнее. Если поток вырастет, начнутся ошибки.",
                  "Traffic surge: {ratio}x more requests than usual. Requests wait in a queue, the server still answers but slower and slower. If traffic grows further, errors will start."),
        "ru": ["На сайт пришло в {ratio} раза больше посетителей, чем обычно. Запросы ждут в очереди, поэтому ответы медленные, и при дальнейшем росте начнутся ошибки.",
               "Это наплыв: запросов в {ratio} раза больше обычного. Сервер пока справляется, но отвечает всё медленнее.",
               "Медленные ответы вызваны потоком посетителей в {ratio} раза больше обычного. Запросы стоят в очереди."],
        "en": ["The site has {ratio} times more visitors than usual. Requests wait in a queue, so responses are slow, and errors will start if traffic keeps growing.",
               "This is a traffic surge: {ratio}x the usual requests. The server still copes, but answers more and more slowly.",
               "The slow responses are caused by {ratio} times the usual traffic. Requests are waiting in a queue."],
    },
    {
        "cause": "slow_dependency", "alarm": "slow-responses",
        "make": lambda r: {"value": r.randint(2100, 9000), "cpu": r.randint(5, 50), "ratio": r1(r.uniform(0.8, 1.3))},
        "title": ("Сайт отвечает медленно: {sec} с", "The website is slow: {sec} s"),
        "draft": ("Нагрузка и CPU в норме, значит тормозит что-то, от чего зависит сайт: база данных или внешний сервис.",
                  "Traffic and CPU are normal, so something the website depends on is slow: the database or an external service."),
        "ru": ["Сервер не перегружен: трафик обычный, CPU на {cpu}%. Значит, тормозит то, от чего зависит сайт, например база данных или внешний сервис.",
               "Сам сервер в порядке, а ответы медленные. Скорее всего, медленно отвечает база данных или внешний API.",
               "Нагрузка и процессор в норме, поэтому причина в зависимостях сайта: базе данных или внешнем сервисе."],
        "en": ["The server is not overloaded: traffic is normal and the CPU is at {cpu}%. Something the site depends on is slow, such as the database or an external service.",
               "The server itself is fine, but responses are slow. The database or an external API is most likely responding slowly.",
               "Load and CPU are normal, so the cause is in the site's dependencies: the database or an external service."],
    },
    {
        "cause": "brute_force", "alarm": "auth-attack",
        "make": lambda r: {"value": r.randint(61, 1500), "nf": r.choice([0, 0, r.randint(30, 600)]), "ratio": r1(r.uniform(1.0, 3.0))},
        "title": ("Похоже на атаку: {value} неудачных входов в минуту", "Possible attack: {value} failed logins per minute"),
        "draft": ("Кто-то массово перебирает пароли.", "Someone is brute-forcing passwords."),
        "ru": ["Кто-то массово подбирает пароли: {value} неудачных попыток входа в минуту.",
               "Это похоже на перебор паролей. Неудачных входов {value} в минуту, обычные пользователи так не ошибаются.",
               "На странице входа идёт атака: пароли перебирают автоматически."],
        "en": ["Someone is guessing passwords at scale: {value} failed logins per minute.",
               "This looks like password brute forcing. There are {value} failed logins per minute, real users do not fail that often.",
               "The login page is under attack: passwords are being guessed automatically."],
        "ru_scan": " Заодно кто-то сканирует сайт, ищет админки и файлы настроек.",
        "en_scan": " Someone is also scanning the site for admin panels and config files.",
    },
    {
        "cause": "verbose_logging", "alarm": "log-flood",
        "make": lambda r: {"value": r.randint(12001, 80000), "errors": 0, "ratio": r1(r.uniform(4, 30))},
        "title": ("Шквал логов: {value} строк в минуту", "Log flood: {value} lines per minute"),
        "draft": ("Логов в разы больше обычного, а ошибок нет. Скорее всего, включён подробный режим (DEBUG). Это само по себе не поломка, но так быстро заканчивается диск.",
                  "There are many times more logs than usual but no errors. Most likely verbose (DEBUG) logging is on. It is not a failure by itself, but it fills the disk fast."),
        "ru": ["Приложение пишет {value} строк логов в минуту, а ошибок нет. Скорее всего, включили подробный режим DEBUG. Сам по себе он не ломает сайт, но быстро забивает диск.",
               "Логов стало в разы больше без всяких ошибок. Похоже, кто-то оставил включённым режим DEBUG.",
               "Это не поломка, а слишком подробное логирование: строк очень много, ошибок нет."],
        "en": ["The application writes {value} log lines per minute with no errors. Verbose DEBUG logging is probably on. It does not break the site by itself, but it fills the disk quickly.",
               "There are many times more logs than usual and no errors. Someone probably left DEBUG logging on.",
               "This is not a failure but overly verbose logging: lots of lines, no errors."],
    },
    {
        "cause": "error_loop", "alarm": "log-flood",
        "make": lambda r: {"value": r.randint(12001, 80000), "errors": r.randint(5, 400), "ratio": r1(r.uniform(4, 30))},
        "title": ("Шквал логов: {value} строк в минуту", "Log flood: {value} lines per minute"),
        "draft": ("Приложение раз за разом пишет одну и ту же ошибку, из-за этого логов очень много.",
                  "The application writes the same error again and again, so there are a lot of logs."),
        "ru": ["Логов так много, потому что приложение снова и снова пишет одну и ту же ошибку.",
               "Шквал логов вызван повторяющейся ошибкой: приложение крутится в цикле и каждый раз её записывает.",
               "Приложение застряло на одной ошибке и пишет её в лог без остановки."],
        "en": ["There are so many logs because the application writes the same error over and over.",
               "The log flood is caused by a repeating error: the application is stuck in a loop and logs it every time.",
               "The application is stuck on one error and keeps writing it to the log."],
    },
    {
        "cause": "app_down", "alarm": "app-down",
        "make": lambda r: {"value": 0, "ratio": r1(r.uniform(0.0, 1.2))},
        "title": ("Приложение не отвечает", "The application does not respond"),
        "draft": ("Сервер работает, но приложение не отвечает на проверку. Логи ещё пишутся, значит процесс жив, но завис или перегружен.",
                  "The server is up, but the application does not answer the health check. Logs are still written, so the process is alive but hung or overloaded."),
        "ru": ["Сервер работает, а приложение не отвечает на проверку здоровья. Процесс ещё жив, но завис или перегружен.",
               "Приложение перестало отвечать, хотя сервер на связи. Похоже, процесс завис.",
               "Проблема в приложении, а не в сервере: проверка здоровья не проходит, процесс завис."],
        "en": ["The server is up, but the application does not answer the health check. The process is still alive but hung or overloaded.",
               "The application stopped responding even though the server is online. The process seems to be hung.",
               "The problem is the application, not the server: the health check fails and the process is hung."],
    },
    {
        "cause": "server_down", "alarm": "server-down",
        "make": lambda r: {"value": 0, "ratio": None},
        "title": ("Сервер не выходит на связь", "The server is not reachable"),
        "draft": ("Агент мониторинга 3 минуты не присылает данные: сервер выключен или перезагружается, пропала сеть, или остановлен сам агент.",
                  "The monitoring agent has sent no data for 3 minutes: the server is off or rebooting, the network is down, or the agent itself has stopped."),
        "ru": ["Агент мониторинга 3 минуты молчит. Сервер выключен или перезагружается, пропала сеть, либо остановился сам агент.",
               "От сервера 3 минуты нет данных. Возможно, он перезагружается, недоступен по сети или агент перестал работать.",
               "Сервер не выходит на связь уже 3 минуты: он выключен, без сети или агент мониторинга остановлен."],
        "en": ["The monitoring agent has been silent for 3 minutes. The server is off or rebooting, the network is down, or the agent itself stopped.",
               "There has been no data from the server for 3 minutes. It may be rebooting, unreachable over the network, or the agent stopped working.",
               "The server has not reported for 3 minutes: it is off, offline, or the monitoring agent has stopped."],
    },
]


def make_example(r, sc, lang):
    v = sc["make"](r)
    value = v["value"]
    sec = round(value / 1000, 1) if sc["alarm"] == "slow-responses" else None
    fill = {"value": value, "ratio": v.get("ratio"), "trend": v.get("trend"), "cpu": v.get("cpu"), "sec": sec}
    stats = {"cpu": v.get("cpu") if v.get("cpu") is not None else r.randint(3, 60) if sc["alarm"] != "cpu-high" else value,
             "mem": value if sc["alarm"] == "mem-high" else r.randint(35, 80),
             "disk": value if sc["alarm"] == "disk-high" else r.randint(20, 70),
             "errors_per_min": v.get("errors", value if sc["alarm"] == "app-errors" else r.choice([0, 0, 1, 2]))}
    if sc["alarm"] == "server-down":
        stats = {"cpu": None, "mem": None, "disk": None, "errors_per_min": None}
    title_ru, title_en = sc["title"]
    facts = {
        "server": r.choice(SERVERS),
        "alarm": sc["alarm"],
        "title": (title_ru if lang == "ru" else title_en).format(**fill),
        "value": value,
        "threshold": {"cpu-high": 80, "mem-high": 90, "disk-high": 85, "app-errors": 5, "slow-responses": 2000,
                      "auth-attack": 60, "log-flood": 12000, "app-down": 0.5, "server-down": 1}[sc["alarm"]],
        "metrics_now": stats,
        "requests_vs_usual": v.get("ratio"),
        "cause_code": sc["cause"],
        "log_messages": v.get("logs", []),
    }
    if sc["alarm"] == "slow-responses":
        facts["latency_s"] = sec
    if sc["alarm"] == "mem-high" and v.get("trend"):
        facts["memory_trend_per_hour"] = v["trend"]
    if sc["alarm"] == "disk-high" and v.get("trend"):
        facts["disk_trend_per_hour"] = v["trend"]
    draft_ru, draft_en = sc["draft"]
    draft = (draft_ru if lang == "ru" else draft_en).format(**fill)
    target = r.choice(sc[lang]).format(**fill)
    if sc["cause"] == "brute_force" and v.get("nf"):
        facts["not_found_per_min"] = v["nf"]
        target += sc[f"{lang}_scan"]
    return {"messages": [
        {"role": "system", "content": SYSTEM[lang]},
        {"role": "user", "content": USER[lang].format(facts=json.dumps(facts, ensure_ascii=False), draft=draft)},
        {"role": "assistant", "content": target},
    ], "meta": {"cause": sc["cause"], "alarm": sc["alarm"], "lang": lang}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=900, help="сколько примеров всего")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--extra", help="JSONL с настоящими примерами (тот же формат), идут в train")
    a = ap.parse_args()
    r = random.Random(a.seed)
    rows = [make_example(r, SCENARIOS[i % len(SCENARIOS)], "ru" if (i // len(SCENARIOS)) % 2 == 0 else "en")
            for i in range(a.n)]
    r.shuffle(rows)
    n_test = n_val = a.n // 10
    parts = {"test": rows[:n_test], "val": rows[n_test:n_test + n_val], "train": rows[n_test + n_val:]}
    if a.extra:
        with open(a.extra) as f:
            parts["train"] += [json.loads(line) for line in f if line.strip()]
    os.makedirs(a.out, exist_ok=True)
    for name, items in parts.items():
        with open(os.path.join(a.out, f"{name}.jsonl"), "w") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        print(f"{name}: {len(items)}")


if __name__ == "__main__":
    main()
