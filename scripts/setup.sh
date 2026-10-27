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
import json
import time
from datetime import datetime, timezone

from flask import Flask, jsonify

LOG_PATH="/var/log/demoapp/app.log"
app=Flask(__name__)
state={"errors_until":0}


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
def break_500():
    state["errors_until"]=time.time()+120
    log("WARN", "break started", kind="500", seconds=120)
    return jsonify(ok=True)


@app.route("/fix", methods=["POST"])
def fix():
    state["errors_until"]=0
    log("INFO", "fixed, back to normal")
    return jsonify(ok=True)
EOF

echo "Flask==3.1.3" > "$APP_DIR/requirements.txt"

# 3. Виртуальное окружение и библиотеки
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
chown -R ec2-user:ec2-user "$APP_DIR" "$LOG_DIR"

# 4. systemd-сервис
cat > /etc/systemd/system/demoapp.service <<'EOF'
[Unit]
Description=Incident demo app
After=network.target

[Service]
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

# 5. CloudWatch Agent
cat > /opt/aws/amazon-cloudwatch-agent/etc/config.json <<EOF
{
  "agent": { "metrics_collection_interval": 60, "run_as_user": "root" },
  "metrics": {
    "append_dimensions": { "InstanceId": "\${aws:InstanceId}" },
    "metrics_collected": {
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

# 6. Проверка
sleep 3
curl -sf localhost:8080/health && echo " <- app ok"
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status
echo "setup.sh done"
