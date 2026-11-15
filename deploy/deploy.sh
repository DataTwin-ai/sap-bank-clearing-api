#!/usr/bin/env bash
# =============================================================================
# deploy.sh - runs ON the EC2 host (sent by CodeBuild through SSM Run Command)
#
#   deploy.sh <image_uri> [aws_region]
#
# 1. Logs in to ECR and pulls <image_uri>
# 2. Refreshes config.ini from SSM Parameter Store (if the parameter exists)
# 3. Replaces the running container
# 4. Health-checks /health; rolls back to the previous image on failure
#
# Tunables (environment variables, all optional):
#   APP_NAME        container name             (default: sap-bank-clearing-api)
#   APP_DIR         host dir for config/logs   (default: /opt/sap-bank-clearing-api)
#   HOST_PORT       host port mapped to 8000   (default: 8000)
#   CONFIG_PARAM    SSM SecureString holding config.ini
#                                              (default: /sap-bank-clearing-api/config.ini)
# =============================================================================
set -euo pipefail

IMAGE_URI="${1:?usage: deploy.sh <image_uri> [aws_region]}"
AWS_REGION="${2:-us-east-1}"
APP_NAME="${APP_NAME:-sap-bank-clearing-api}"
APP_DIR="${APP_DIR:-/opt/sap-bank-clearing-api}"
HOST_PORT="${HOST_PORT:-8000}"
CONFIG_PARAM="${CONFIG_PARAM:-/sap-bank-clearing-api/config.ini}"
REGISTRY="${IMAGE_URI%%/*}"

log() { echo "[deploy $(date -u +%H:%M:%S)] $*"; }

command -v docker >/dev/null || { log "ERROR: docker is not installed on this host"; exit 1; }
command -v aws    >/dev/null || { log "ERROR: aws cli is not installed on this host"; exit 1; }

# --- host directories (container runs as uid 1000) ---------------------------
mkdir -p "$APP_DIR/log" "$APP_DIR/data"
chown -R 1000:1000 "$APP_DIR/log" "$APP_DIR/data"

# --- config.ini --------------------------------------------------------------
if aws ssm get-parameter --region "$AWS_REGION" --name "$CONFIG_PARAM" \
      --with-decryption --query Parameter.Value --output text > "$APP_DIR/config.ini.new" 2>/dev/null; then
  mv "$APP_DIR/config.ini.new" "$APP_DIR/config.ini"
  log "config.ini refreshed from SSM parameter $CONFIG_PARAM"
else
  rm -f "$APP_DIR/config.ini.new"
  log "SSM parameter $CONFIG_PARAM not readable; using existing $APP_DIR/config.ini"
fi
[ -s "$APP_DIR/config.ini" ] || { log "ERROR: $APP_DIR/config.ini missing"; exit 1; }
chmod 640 "$APP_DIR/config.ini"
chown root:1000 "$APP_DIR/config.ini"

# --- pull ----------------------------------------------------------------------
log "Logging in to $REGISTRY"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"
log "Pulling $IMAGE_URI"
docker pull "$IMAGE_URI"

PREV_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$APP_NAME" 2>/dev/null || true)"

run_container() {
  docker rm -f "$APP_NAME" >/dev/null 2>&1 || true
  docker run -d \
    --name "$APP_NAME" \
    --restart unless-stopped \
    -p "${HOST_PORT}:8000" \
    -v "$APP_DIR/config.ini:/app/config.ini:ro" \
    -v "$APP_DIR/log:/app/log" \
    -v "$APP_DIR/data:/app/data" \
    --log-driver json-file --log-opt max-size=20m --log-opt max-file=5 \
    "$1" >/dev/null
}

healthy() {
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${HOST_PORT}/health" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

log "Starting $APP_NAME from $IMAGE_URI on port $HOST_PORT"
run_container "$IMAGE_URI"

if healthy; then
  log "Deployment healthy"
  docker image prune -af --filter "until=168h" >/dev/null 2>&1 || true
  exit 0
fi

log "ERROR: health check failed. Container logs:"
docker logs --tail 80 "$APP_NAME" || true
if [ -n "$PREV_IMAGE" ] && [ "$PREV_IMAGE" != "$IMAGE_URI" ]; then
  log "Rolling back to $PREV_IMAGE"
  run_container "$PREV_IMAGE"
  healthy && log "Rollback healthy" || log "ERROR: rollback also unhealthy"
fi
exit 1
