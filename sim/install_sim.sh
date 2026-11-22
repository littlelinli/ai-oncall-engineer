#!/bin/bash
# Установка симулятора живого сайта ShopSim на демо-сервер (Amazon Linux 2023).
# Заменяет старое демо-приложение на порту 8080, сохраняя его токен, лог и ручки /break и /fix.
#
#   sudo bash install_sim.sh               приложение + посетители + сбор датасета
#   sudo bash install_sim.sh --no-traffic  без генератора (если посетители идут с другого сервера)
#
# Настройки генератора можно поменять потом в /etc/shopsim.env и перезапустить:
#   sudo systemctl restart shopsim-traffic
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP_DIR=/opt/shopsim
DATA_DIR=/var/lib/shopsim
LOG_DIR=/var/log/demoapp
RUN_USER=ec2-user
TRAFFIC=1
[ "${1:-}" = "--no-traffic" ] && TRAFFIC=0

if [ "$(id -u)" -ne 0 ]; then echo "Запустите через sudo" >&2; exit 1; fi
for f in app.py gunicorn.conf.py traffic.py collector.py; do
  [ -f "$SRC/$f" ] || { echo "Нет файла $SRC/$f" >&2; exit 1; }
done

# 1. Пакеты
dnf install -y python3-pip logrotate >/dev/null

# 2. Код и окружение
mkdir -p "$APP_DIR" "$DATA_DIR/uploads" "$DATA_DIR/dataset" "$LOG_DIR"
cp "$SRC"/{app.py,gunicorn.conf.py,traffic.py,collector.py} "$APP_DIR/"
[ -x "$APP_DIR/venv/bin/python" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q "Flask==3.1.3" "gunicorn==23.0.0"
chown -R "$RUN_USER:$RUN_USER" "$APP_DIR" "$DATA_DIR" "$LOG_DIR"

# 3. Токен демо-приложения: берём существующий, иначе создаём
if [ ! -s /etc/demoapp.env ]; then
  echo "DEMO_TOKEN=$(openssl rand -hex 16)" > /etc/demoapp.env
fi
chmod 600 /etc/demoapp.env

# 4. Настройки симулятора (не перезаписываем, если уже есть)
if [ ! -f /etc/shopsim.env ]; then
  cat > /etc/shopsim.env <<'EOF'
SIM_DATA=/var/lib/shopsim
SIM_LOG=/var/log/demoapp/app.log
SIM_TARGET=http://127.0.0.1:8080
# посетителей в секунду в вечерний пик (0.8 даёт около 8 запросов в секунду)
SIM_BASE_SESSIONS=0.8
# как часто в среднем включать проблему и наплыв, в часах
SIM_INCIDENT_EVERY_H=3
SIM_FLASH_EVERY_H=2.5
SIM_INCIDENTS=1
# бакет S3 для выгрузки датасета (пусто = не выгружать)
SIM_S3_BUCKET=
EOF
fi

# 5. База магазина
sudo -u "$RUN_USER" env SIM_DATA="$DATA_DIR" SIM_LOG="$LOG_DIR/app.log" \
  "$APP_DIR/venv/bin/python" "$APP_DIR/app.py" --init-db

# 6. Сервисы
cat > /etc/systemd/system/demoapp.service <<EOF
[Unit]
Description=ShopSim demo shop (Kezekshi)
After=network.target

[Service]
User=$RUN_USER
EnvironmentFile=/etc/demoapp.env
EnvironmentFile=/etc/shopsim.env
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/gunicorn -c $APP_DIR/gunicorn.conf.py app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/shopsim-collector.service <<EOF
[Unit]
Description=ShopSim dataset collector
After=demoapp.service

[Service]
User=$RUN_USER
EnvironmentFile=/etc/shopsim.env
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/collector.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/shopsim-traffic.service <<EOF
[Unit]
Description=ShopSim traffic generator
After=demoapp.service
Requires=demoapp.service

[Service]
User=$RUN_USER
EnvironmentFile=/etc/demoapp.env
EnvironmentFile=/etc/shopsim.env
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/traffic.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# 7. Ротация лога, чтобы диск не забивался по-настоящему
cat > /etc/logrotate.d/shopsim <<EOF
$LOG_DIR/app.log {
    daily
    maxsize 200M
    rotate 3
    compress
    missingok
    notifempty
    copytruncate
}
EOF
systemctl enable --now logrotate.timer >/dev/null 2>&1 || true

systemctl daemon-reload
systemctl enable demoapp shopsim-collector >/dev/null
systemctl restart demoapp
sleep 3
systemctl restart shopsim-collector
if [ "$TRAFFIC" = "1" ]; then
  systemctl enable shopsim-traffic >/dev/null
  systemctl restart shopsim-traffic
else
  systemctl disable --now shopsim-traffic >/dev/null 2>&1 || true
fi

# 8. Проверка
sleep 2
curl -sf localhost:8080/health >/dev/null && echo "app: ok" || echo "app: НЕ ОТВЕЧАЕТ, смотрите journalctl -u demoapp -n 30"
for s in demoapp shopsim-collector shopsim-traffic; do
  printf '%-20s %s\n' "$s" "$(systemctl is-active $s || true)"
done
echo "Датасет: $DATA_DIR/dataset/metrics-*.csv (новая строка раз в минуту)"
echo "Истинные причины: $DATA_DIR/events.jsonl"
echo "shopsim install done"
