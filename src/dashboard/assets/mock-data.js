// Тестовые инциденты в том же формате, который будет отдавать API (GET /incidents).
// Даты считаются от текущего момента, чтобы график всегда выглядел свежим.
(function () {
  const now = Date.now();
  const at = (daysAgo, hhmm) => {
    const d = new Date(now);
    d.setDate(d.getDate() - daysAgo);
    const [h, m] = hhmm.split(":").map(Number);
    d.setHours(h, m, 0, 0);
    return d.toISOString();
  };

  window.MOCK_INCIDENTS = [
    {
      incident_id: "inc-20261026-0312",
      title: "Резкий рост ошибок 500 на demo-app",
      severity: "critical",
      status: "open",
      started_at: at(0, "03:12"),
      resolved_at: null,
      alarm_name: "demo-app-HTTP5xxRate",
      service: "demo-app",
      instance_id: "i-0a1b2c3d4e5f",
      summary: "За 3 минуты доля ответов 500 выросла с 0,4% до 38%. Ошибки идут на всех эндпоинтах, которые обращаются к базе.",
      root_cause: "Закончились свободные соединения в пуле базы данных. Новые запросы ждут соединения 5 секунд и падают по таймауту, отсюда ответы 500.",
      recommendations: [
        "Перезапустить сервис demo-app, чтобы освободить зависшие соединения",
        "Увеличить размер пула с 10 до 25 соединений",
        "Добавить таймаут на долгие запросы к базе, чтобы соединения не висели"
      ],
      stats: { cpu_percent: 41, error_rate_percent: 38, disk_used_percent: 52, p95_latency_ms: 5120 },
      logs: [
        "03:09:58 INFO  GET /orders 200 84ms",
        "03:11:02 WARN  db pool usage 9/10",
        "03:11:40 WARN  db pool usage 10/10, waiting for connection",
        "03:12:05 ERROR TimeoutError: could not acquire connection in 5000ms",
        "03:12:05 ERROR GET /orders 500 5003ms",
        "03:12:07 ERROR GET /checkout 500 5001ms"
      ],
      auto_fix: { applied: false, action: null, result: "Ожидает решения: перезапуск разрешён, но выполняется после подтверждения" },
      report_url: null
    },
    {
      incident_id: "inc-20261025-2210",
      title: "Процесс приложения перестал отвечать",
      severity: "high",
      status: "resolved",
      started_at: at(1, "22:10"),
      resolved_at: at(1, "22:13"),
      alarm_name: "demo-app-HealthCheckFailed",
      service: "demo-app",
      instance_id: "i-0a1b2c3d4e5f",
      summary: "Эндпоинт /health не отвечал 2 минуты подряд, при этом сервер работал и CPU был почти нулевым.",
      root_cause: "Процесс приложения завис: основной поток заблокирован, новые запросы не обрабатываются. Сервер и сеть в порядке.",
      recommendations: [
        "Перезапустить сервис через systemctl",
        "Включить watchdog в systemd, чтобы зависший процесс перезапускался сам",
        "Найти в коде операцию, которая блокирует поток (вызов /hang в демо)"
      ],
      stats: { cpu_percent: 2, error_rate_percent: 100, disk_used_percent: 52, p95_latency_ms: 30000 },
      logs: [
        "22:08:11 INFO  GET /health 200 3ms",
        "22:09:30 INFO  GET /hang called",
        "22:10:00 ERROR health check timeout after 5000ms",
        "22:11:00 ERROR health check timeout after 5000ms",
        "22:12:41 INFO  ssm: systemctl restart demo-app",
        "22:12:44 INFO  demo-app started, GET /health 200 4ms"
      ],
      auto_fix: { applied: true, action: "systemctl restart demo-app", result: "Сервис перезапущен через SSM, проверка здоровья проходит" },
      report_url: null
    },
    {
      incident_id: "inc-20261024-1405",
      title: "Диск сервера заполнен на 94%",
      severity: "high",
      status: "resolved",
      started_at: at(2, "14:03"),
      resolved_at: at(2, "14:58"),
      alarm_name: "demo-app-DiskUsedHigh",
      service: "demo-app",
      instance_id: "i-0a1b2c3d4e5f",
      summary: "Свободное место на корневом разделе упало с 48% до 6% за 20 минут.",
      root_cause: "Приложение пишет большие временные файлы в /tmp/demo и не удаляет их. Логи приложения тоже не ротируются.",
      recommendations: [
        "Удалить временные файлы в /tmp/demo",
        "Настроить logrotate для логов приложения",
        "Добавить очистку временных файлов после обработки запроса"
      ],
      stats: { cpu_percent: 18, error_rate_percent: 3, disk_used_percent: 94, p95_latency_ms: 310 },
      logs: [
        "13:44:10 INFO  POST /disk called, writing 500MB",
        "13:52:31 WARN  disk usage 81%",
        "14:03:58 WARN  disk usage 90%",
        "14:05:12 ERROR OSError: [Errno 28] No space left on device"
      ],
      auto_fix: { applied: false, action: null, result: "Не применялась: удаление файлов не входит в список разрешённых действий" },
      report_url: null
    },
    {
      incident_id: "inc-20261022-1030",
      title: "Загрузка CPU выше 90% больше 5 минут",
      severity: "medium",
      status: "resolved",
      started_at: at(4, "10:31"),
      resolved_at: at(4, "10:52"),
      alarm_name: "demo-app-CPUHigh",
      service: "demo-app",
      instance_id: "i-0a1b2c3d4e5f",
      summary: "CPU держался на 96–99% в течение 7 минут, время ответа выросло в 6 раз.",
      root_cause: "Запущена тяжёлая вычислительная задача (/cpu), которая занимает все ядра. Внешней нагрузки по количеству запросов нет.",
      recommendations: [
        "Остановить тяжёлую задачу или вынести её в фоновую очередь",
        "Ограничить время выполнения тяжёлых запросов",
        "Рассмотреть автоматическое масштабирование через Auto Scaling Group"
      ],
      stats: { cpu_percent: 98, error_rate_percent: 1, disk_used_percent: 47, p95_latency_ms: 1840 },
      logs: [
        "10:28:02 INFO  GET /cpu called, duration=600s",
        "10:29:40 WARN  request latency 1.2s on GET /orders",
        "10:31:15 WARN  request latency 1.9s on GET /orders"
      ],
      auto_fix: { applied: false, action: null, result: "Не применялась: проблема не входит в список типовых" },
      report_url: null
    },
    {
      incident_id: "inc-20261019-0845",
      title: "Кратковременный рост ошибок 500",
      severity: "low",
      status: "resolved",
      started_at: at(7, "08:44"),
      resolved_at: at(7, "08:48"),
      alarm_name: "demo-app-HTTP5xxRate",
      service: "demo-app",
      instance_id: "i-0a1b2c3d4e5f",
      summary: "Ошибки 500 на 4 минуты поднялись до 6% и затем пропали сами.",
      root_cause: "Ошибки шли только с эндпоинта /error, который вызывали вручную. Остальные эндпоинты работали без ошибок.",
      recommendations: [
        "Действий не требуется, сбой вызван тестовым эндпоинтом",
        "Исключить /error из расчёта тревоги в продакшене"
      ],
      stats: { cpu_percent: 12, error_rate_percent: 6, disk_used_percent: 45, p95_latency_ms: 120 },
      logs: [
        "08:44:10 ERROR GET /error 500 intentional failure",
        "08:44:12 ERROR GET /error 500 intentional failure",
        "08:44:20 INFO  GET /orders 200 72ms"
      ],
      auto_fix: { applied: false, action: null, result: "Не требовалась" },
      report_url: null
    },
    {
      incident_id: "inc-20261016-1912",
      title: "Сервер не прошёл проверку статуса EC2",
      severity: "critical",
      status: "resolved",
      started_at: at(10, "19:12"),
      resolved_at: at(10, "19:34"),
      alarm_name: "demo-app-StatusCheckFailed",
      service: "demo-app",
      instance_id: "i-0a1b2c3d4e5f",
      summary: "AWS сообщил об ошибке проверки статуса инстанса, приложение было недоступно 20 минут.",
      root_cause: "На сервере закончилась оперативная память, ядро завершило процесс приложения, система перестала отвечать на проверки.",
      recommendations: [
        "Перезагрузить инстанс",
        "Добавить swap или выбрать тип инстанса с большей памятью",
        "Включить тревогу по памяти через CloudWatch Agent"
      ],
      stats: { cpu_percent: 77, error_rate_percent: 100, disk_used_percent: 49, p95_latency_ms: 30000 },
      logs: [
        "19:10:44 WARN  memory usage 92%",
        "19:11:58 ERROR Out of memory: Killed process 1432 (python3)",
        "19:12:30 ERROR StatusCheckFailed_Instance = 1"
      ],
      auto_fix: { applied: false, action: null, result: "Не применялась: перезагрузка инстанса требует подтверждения" },
      report_url: null
    }
  ];
})();
