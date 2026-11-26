#!/usr/bin/env bash
# Server setup for sap-extraction-api (systemd Linux, e.g. Amazon Linux 2023 / Ubuntu).
# Run from the extracted project folder, as root:
#     sudo bash deploy/install.sh
# Re-runnable: an update is "extract the new bundle over the folder, run this again".
#
#   1. service user (APP_USER, default sapapp)
#   2. .venv + requirements.txt
#   3. log/ and data/ folders, ownership, config.ini readable by the service user only
#   4. connectivity check: SAP OData + PostgreSQL (stops here if it fails)
#   5. delta tables status (BSID / BSAD must have been delta-init'ed once - see docs/DEPLOYMENT.md)
#   6. systemd unit sap-extraction-api (port 8002, local only), (re)started
#   7. the 1-minute delta sync, one of (DELTA_MODE):
#        worker (default) - systemd unit sap-extraction-worker (long-running loop)
#        cron             - /etc/cron.d/sap-extraction-delta runs `cli.py delta --cron` every minute;
#                           the worker unit is stopped and disabled (never run both)
#
#   optional: CLOUDWATCH=true installs the CloudWatch agent and ships log/*.log + log/cron.out to
#             log group CW_LOG_GROUP (default /sap-extraction-api), kept CW_RETENTION_DAYS (30)
#
#   sudo DELTA_MODE=cron CLOUDWATCH=true bash deploy/install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${APP_USER:-sapapp}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DELTA_MODE="${DELTA_MODE:-worker}"
CLOUDWATCH="${CLOUDWATCH:-false}"
CW_LOG_GROUP="${CW_LOG_GROUP:-/sap-extraction-api}"
CW_RETENTION_DAYS="${CW_RETENTION_DAYS:-30}"
cd "$APP_DIR"

case "$DELTA_MODE" in
  worker|cron) ;;
  *) echo "DELTA_MODE must be 'worker' or 'cron' (got '$DELTA_MODE')" >&2; exit 1 ;;
esac

say() { printf '\n==> %s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo bash deploy/install.sh" >&2
  exit 1
fi
if [ ! -f config.ini ]; then
  echo "config.ini is missing in $APP_DIR - copy it next to cli.py first (template: config.example.ini)" >&2
  exit 1
fi
if [ "$APP_DIR" != "/opt/sap-extraction-api" ]; then
  echo "Note: the systemd units expect /opt/sap-extraction-api (this folder: $APP_DIR)." >&2
  echo "      Edit deploy/*.service first if you install somewhere else." >&2
fi

say "1/7 service user '$APP_USER'"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /sbin/nologin "$APP_USER"
  echo "created"
else
  echo "exists"
fi

say "2/7 virtualenv + packages"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ required"'
[ -x .venv/bin/python ] || "$PYTHON_BIN" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
.venv/bin/python -c 'import fastapi, pandas, psycopg2, requests, sqlalchemy, uvicorn; print("packages ok")'

say "3/7 folders and permissions"
mkdir -p log data
chmod +x deploy/run_cron.sh deploy/install.sh
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
chmod 600 config.ini
[ -f .env ] && chmod 600 .env
echo "ok"

as_app() { sudo -u "$APP_USER" -H "$APP_DIR/.venv/bin/python" "$APP_DIR/cli.py" "$@"; }

say "4/7 SAP + database connectivity"
if ! as_app check; then
  echo "Connectivity check failed - fix config.ini / security groups, then run this script again." >&2
  exit 1
fi

say "5/7 delta tables"
as_app delta-status || echo "delta-status failed - see docs/DEPLOYMENT.md step 'Delta tables'"

say "6/7 systemd service sap-extraction-api"
install -m 644 deploy/sap-extraction-api.service /etc/systemd/system/sap-extraction-api.service
install -m 644 deploy/sap-extraction-worker.service /etc/systemd/system/sap-extraction-worker.service
systemctl daemon-reload
systemctl enable sap-extraction-api >/dev/null
systemctl restart sap-extraction-api

CRON_FILE=/etc/cron.d/sap-extraction-delta
say "7/7 delta sync every minute: $DELTA_MODE"
if [ "$DELTA_MODE" = "cron" ]; then
  # never both: the worker and the cron would poll and ingest side by side
  systemctl disable --now sap-extraction-worker >/dev/null 2>&1 || true
  # Amazon Linux 2023 ships without cron; Ubuntu has it
  if ! command -v crond >/dev/null 2>&1 && ! command -v cron >/dev/null 2>&1; then
    if command -v dnf >/dev/null 2>&1; then dnf install -y -q cronie; else apt-get install -y -qq cron; fi
  fi
  if command -v crond >/dev/null 2>&1; then CRON_UNIT=crond; else CRON_UNIT=cron; fi
  systemctl enable --now "$CRON_UNIT" >/dev/null
  cat > "$CRON_FILE" <<EOF
# Installed by deploy/install.sh (DELTA_MODE=cron). SAP BSID/BSAD/VBRK/VBRP -> delta queue -> ingestion API.
# Details: $APP_DIR/log/cron.log ; one JSON line per run: $APP_DIR/log/cron.out
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
* * * * * $APP_USER $APP_DIR/deploy/run_cron.sh delta --cron >> $APP_DIR/log/cron.out 2>&1
EOF
  chmod 644 "$CRON_FILE"
  cat > /etc/logrotate.d/sap-extraction-cron <<EOF
$APP_DIR/log/cron.out {
    su $APP_USER $APP_USER
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}
EOF
  echo "cron installed: $CRON_FILE ($CRON_UNIT)"
else
  rm -f "$CRON_FILE"
  systemctl enable sap-extraction-worker >/dev/null
  systemctl restart sap-extraction-worker
fi
if [ "$CLOUDWATCH" = "true" ]; then
  say "CloudWatch Logs: log group $CW_LOG_GROUP"
  AGENT_CTL=/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl
  if [ ! -x "$AGENT_CTL" ]; then
    if command -v dnf >/dev/null 2>&1; then
      dnf install -y -q amazon-cloudwatch-agent
    else
      curl -fsSL -o /tmp/amazon-cloudwatch-agent.deb \
        "https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/$(dpkg --print-architecture)/latest/amazon-cloudwatch-agent.deb"
      dpkg -i -E /tmp/amazon-cloudwatch-agent.deb && rm -f /tmp/amazon-cloudwatch-agent.deb
    fi
  fi
  CW_CONFIG="$APP_DIR/deploy/cloudwatch-agent.json"
  # cron.log / worker.log / api.log: one event per log record (tracebacks stay with their line);
  # cron.out: one JSON line per cron run that moved data or failed
  cat > "$CW_CONFIG" <<EOF
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {"file_path": "$APP_DIR/log/cron.log", "log_group_name": "$CW_LOG_GROUP",
           "log_stream_name": "{instance_id}/cron.log", "retention_in_days": $CW_RETENTION_DAYS,
           "timestamp_format": "%Y-%m-%d %H:%M:%S", "timezone": "Local",
           "multi_line_start_pattern": "{timestamp_format}"},
          {"file_path": "$APP_DIR/log/cron.out", "log_group_name": "$CW_LOG_GROUP",
           "log_stream_name": "{instance_id}/cron.out", "retention_in_days": $CW_RETENTION_DAYS},
          {"file_path": "$APP_DIR/log/worker.log", "log_group_name": "$CW_LOG_GROUP",
           "log_stream_name": "{instance_id}/worker.log", "retention_in_days": $CW_RETENTION_DAYS,
           "timestamp_format": "%Y-%m-%d %H:%M:%S", "timezone": "Local",
           "multi_line_start_pattern": "{timestamp_format}"},
          {"file_path": "$APP_DIR/log/api.log", "log_group_name": "$CW_LOG_GROUP",
           "log_stream_name": "{instance_id}/api.log", "retention_in_days": $CW_RETENTION_DAYS,
           "timestamp_format": "%Y-%m-%d %H:%M:%S", "timezone": "Local",
           "multi_line_start_pattern": "{timestamp_format}"}
        ]
      }
    }
  }
}
EOF
  # append-config keeps any other agent configuration already on this host; running it again
  # with the same file replaces this file's part only
  "$AGENT_CTL" -a append-config -m ec2 -s -c "file:$CW_CONFIG"
  echo "CloudWatch agent running; logs appear in $CW_LOG_GROUP once written"
  echo "(the EC2 role needs logs:CreateLogGroup/CreateLogStream/PutLogEvents/PutRetentionPolicy - see docs/AWS_CRON_SETUP.md)"
fi

sleep 3
systemctl --no-pager --lines=0 status sap-extraction-api || true
if [ "$DELTA_MODE" = "worker" ]; then
  systemctl --no-pager --lines=0 status sap-extraction-worker || true
fi

say "done"
echo "API health : curl -s http://127.0.0.1:8002/health"
if [ "$DELTA_MODE" = "cron" ]; then
  echo "Cron runs  : tail -f $APP_DIR/log/cron.out      (details: $APP_DIR/log/cron.log)"
else
  echo "Worker log : tail -f $APP_DIR/log/worker.log"
fi
echo "Queue      : sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/cli.py delta-status"
