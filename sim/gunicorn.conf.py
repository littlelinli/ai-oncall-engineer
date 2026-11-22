# Ограниченная ёмкость как у реального небольшого сервера:
# 2 процесса по 4 потока = 8 запросов одновременно, остальные ждут в очереди.
bind = "0.0.0.0:8080"
workers = 2
worker_class = "gthread"
threads = 4
backlog = 64
timeout = 30
graceful_timeout = 10
accesslog = None
errorlog = "-"
loglevel = "warning"
