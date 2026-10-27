#!/bin/bash
# Установка демо-приложения и CloudWatch Agent на Amazon Linux 2023.
# Можно запускать вручную (sudo bash setup.sh) или как EC2 user_data.
# Скрипт можно запускать повторно: он перезапишет файлы и перезапустит сервисы.
set -euo pipefail

APP_DIR=/opt/demoapp
LOG_DIR=/var/log/demoapp
LOG_GROUP=/demoapp/app

# 1. Пакеты
dnf install -y python3-pip amazon-cloudwatch-agent

# 2. Папки и код приложения
mkdir -p "$APP_DIR" "$LOG_DIR"

cat > "$APP_DIR/app.py" <<'EOF'
import hmac
import json
import multiprocessing
import os
import shutil
import time
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, jsonify, request

LOG_PATH="/var/log/demoapp/app.log"
FILL_PATH="/var/tmp/demoapp-fill.bin"
CPU_SECONDS=300
DISK_TARGET=0.92
DEMO_TOKEN=os.environ.get("DEMO_TOKEN", "")
app=Flask(__name__)
state={"errors_until":0}
cpu_procs=[]


def require_token(view):
    # Ручки поломок работают только с токеном: заголовок X-Demo-Token или ?token=
    @wraps(view)
    def wrapper(*args, **kwargs):
        given=request.headers.get("X-Demo-Token") or request.args.get("token") or ""
        if not DEMO_TOKEN or not hmac.compare_digest(given, DEMO_TOKEN):
            log("WARN", "rejected request without valid token", path=request.path)
            return jsonify(error="forbidden"), 403
        return view(*args, **kwargs)
    return wrapper


def log(level, msg, **extra):
    row={"time": datetime.now(timezone.utc).isoformat(), "level": level, "msg": msg}
    row.update(extra)
    line=json.dumps(row)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


@app.route("/health")
def health():
    return "ok"


@app.route("/search")
def search():
    if time.time()<state["errors_until"]:
        log("ERROR", "database connection refused", path="/search", status=500)
        return jsonify(error="internal error"), 500
    log("INFO", "request ok", path="/search", status=200)
    return jsonify(ok=True)


@app.route("/break/500", methods=["POST"])
@require_token
def break_500():
    state["errors_until"]=time.time()+120
    log("WARN", "break started", kind="500", seconds=120)
    return jsonify(ok=True)


def burn(seconds):
    end=time.time()+seconds
    while time.time()<end:
        pass


def stop_cpu():
    for p in cpu_procs:
        if p.is_alive():
            p.terminate()
    cpu_procs.clear()


def disk_percent():
    total, used, free = shutil.disk_usage("/")
    return round(used * 100 / total, 1)


@app.route("/break/cpu", methods=["POST"])
@require_token
def break_cpu():
    stop_cpu()
    cores=os.cpu_count() or 1
    for _ in range(cores):
        p=multiprocessing.Process(target=burn, args=(CPU_SECONDS,), daemon=True)
        p.start()
        cpu_procs.append(p)
    log("WARN", "break started", kind="cpu", seconds=CPU_SECONDS, cores=cores)
    return jsonify(ok=True)


@app.route("/break/disk", methods=["POST"])
@require_token
def break_disk():
    if os.path.exists(FILL_PATH):
        os.remove(FILL_PATH)
    total, used, free = shutil.disk_usage("/")
    size=int(total * DISK_TARGET) - used
    if size > 0:
        with open(FILL_PATH, "wb") as f:
            os.posix_fallocate(f.fileno(), 0, size)
    log("WARN", "break started", kind="disk", used_percent=disk_percent())
    return jsonify(ok=True)


@app.route("/fix", methods=["POST"])
@require_token
def fix():
    state["errors_until"]=0
    stop_cpu()
    if os.path.exists(FILL_PATH):
        os.remove(FILL_PATH)
    log("INFO", "fixed, back to normal", used_percent=disk_percent())
    return jsonify(ok=True)
EOF

echo "Flask==3.1.3" > "$APP_DIR/requirements.txt"

# 3. Виртуальное окружение и библиотеки
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
chown -R ec2-user:ec2-user "$APP_DIR" "$LOG_DIR"

# 4. Токен для ручек поломок (создаётся один раз, при повторном запуске сохраняется)
if [ ! -s /etc/demoapp.env ]; then
  echo "DEMO_TOKEN=$(openssl rand -hex 16)" > /etc/demoapp.env
fi
chmod 600 /etc/demoapp.env

# 5. systemd-сервис
cat > /etc/systemd/system/demoapp.service <<'EOF'
[Unit]
Description=Incident demo app
After=network.target

[Service]
EnvironmentFile=/etc/demoapp.env
User=ec2-user
WorkingDirectory=/opt/demoapp
ExecStart=/opt/demoapp/venv/bin/python -m flask --app app run --host 0.0.0.0 --port 8080
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable demoapp
systemctl restart demoapp

# 6. CloudWatch Agent
cat > /opt/aws/amazon-cloudwatch-agent/etc/config.json <<EOF
{
  "agent": { "metrics_collection_interval": 60, "run_as_user": "root" },
  "metrics": {
    "append_dimensions": { "InstanceId": "\${aws:InstanceId}" },
    "metrics_collected": {
      "cpu":  { "measurement": ["usage_active"], "totalcpu": true },
      "disk": { "measurement": ["used_percent"], "resources": ["/"] },
      "mem":  { "measurement": ["mem_used_percent"] }
    }
  },
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "$LOG_DIR/app.log",
            "log_group_name": "$LOG_GROUP",
            "log_stream_name": "{instance_id}"
          }
        ]
      }
    }
  }
}
EOF

/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -s \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/config.json

# 7. Проверка
sleep 3
curl -sf localhost:8080/health && echo " <- app ok"
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status
echo "DEMO TOKEN: $(cut -d= -f2 /etc/demoapp.env)"
echo "setup.sh done"
