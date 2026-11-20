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
#   6. systemd units sap-extraction-api (port 8002, local only) + sap-extraction-worker, (re)started
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${APP_USER:-sapapp}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
cd "$APP_DIR"

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

say "1/6 service user '$APP_USER'"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /sbin/nologin "$APP_USER"
  echo "created"
else
  echo "exists"
fi

say "2/6 virtualenv + packages"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ required"'
[ -x .venv/bin/python ] || "$PYTHON_BIN" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
.venv/bin/python -c 'import fastapi, pandas, psycopg2, requests, sqlalchemy, uvicorn; print("packages ok")'

say "3/6 folders and permissions"
mkdir -p log data
chmod +x deploy/run_cron.sh deploy/install.sh
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
chmod 600 config.ini
[ -f .env ] && chmod 600 .env
echo "ok"

as_app() { sudo -u "$APP_USER" -H "$APP_DIR/.venv/bin/python" "$APP_DIR/cli.py" "$@"; }

say "4/6 SAP + database connectivity"
if ! as_app check; then
  echo "Connectivity check failed - fix config.ini / security groups, then run this script again." >&2
  exit 1
fi

say "5/6 delta tables"
as_app delta-status || echo "delta-status failed - see docs/DEPLOYMENT.md step 'Delta tables'"

say "6/6 systemd services"
install -m 644 deploy/sap-extraction-api.service /etc/systemd/system/sap-extraction-api.service
install -m 644 deploy/sap-extraction-worker.service /etc/systemd/system/sap-extraction-worker.service
systemctl daemon-reload
systemctl enable sap-extraction-api sap-extraction-worker >/dev/null
systemctl restart sap-extraction-api sap-extraction-worker
sleep 3
systemctl --no-pager --lines=0 status sap-extraction-api sap-extraction-worker || true

say "done"
echo "API health : curl -s http://127.0.0.1:8002/health"
echo "Worker log : tail -f $APP_DIR/log/worker.log"
echo "Queue      : sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/cli.py delta-status"
