#!/usr/bin/env bash
# Cron wrapper: runs cli.py from the project folder with the project's virtualenv.
#   deploy/run_cron.sh               -> python cli.py sync
#   deploy/run_cron.sh sync-bt3i     -> python cli.py sync-bt3i
# Optional environment file: <project>/.env (KEY=VALUE lines, e.g. DB_PASSWORD=...)
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

if [ -f "$APP_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$APP_DIR/.env"
  set +a
fi

PYTHON="${PYTHON:-$APP_DIR/.venv/bin/python}"
if [ $# -eq 0 ]; then
  set -- sync
fi

exec "$PYTHON" cli.py "$@"
