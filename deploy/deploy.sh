#!/usr/bin/env bash
# =============================================================================
# deploy.sh - runs ON the EC2 host (sent by CodeBuild through SSM Run Command)
#
#   deploy.sh <bundle_s3_uri> <release> [aws_region]
#
# 1. Downloads the source bundle from S3 and extracts it
# 2. Builds the Docker image locally (sap-bank-clearing-api:<release>)
# 3. Installs config.ini (SSM Parameter Store, else the file already on the host)
# 4. Replaces the running container
# 5. Health-checks /health; rolls back to the previous image on failure
#
# Tunables (environment variables, all optional):
#   APP_NAME        container/image name       (default: sap-bank-clearing-api)
#   APP_DIR         host dir for config/logs   (default: /opt/sap-bank-clearing-api)
#   HOST_PORT       host port mapped to 8000   (default: 8000)
#   BIND_ADDR       host address to bind       (default: 127.0.0.1 - only nginx reaches it)
#   ROOT_PATH       URL prefix behind nginx    (default: /sap/bank)
#   CONFIG_PARAM    SSM SecureString holding config.ini
#                                              (default: /sap-bank-clearing-api/config.ini)
#   KEEP_RELEASES   releases/images to keep    (default: 5)
# =============================================================================
set -euo pipefail

BUNDLE_URI="${1:?usage: deploy.sh <bundle_s3_uri> <release> [aws_region]}"
RELEASE="${2:?usage: deploy.sh <bundle_s3_uri> <release> [aws_region]}"
AWS_REGION="${3:-us-east-1}"
APP_NAME="${APP_NAME:-sap-bank-clearing-api}"
APP_DIR="${APP_DIR:-/opt/sap-bank-clearing-api}"
HOST_PORT="${HOST_PORT:-8000}"
BIND_ADDR="${BIND_ADDR:-127.0.0.1}"
ROOT_PATH="${ROOT_PATH-/sap/bank}"
CONFIG_PARAM="${CONFIG_PARAM:-/sap-bank-clearing-api/config.ini}"
KEEP_RELEASES="${KEEP_RELEASES:-5}"
IMAGE="${APP_NAME}:${RELEASE}"
RELEASE_DIR="$APP_DIR/releases/$RELEASE"

log() { echo "[deploy $(date -u +%H:%M:%S)] $*"; }

command -v docker >/dev/null || { log "ERROR: docker is not installed on this host"; exit 1; }
command -v aws    >/dev/null || { log "ERROR: aws cli is not installed on this host"; exit 1; }

# --- host directories (container runs as uid 1000) ---------------------------
mkdir -p "$APP_DIR/log" "$APP_DIR/data" "$APP_DIR/releases"
chown -R 1000:1000 "$APP_DIR/log" "$APP_DIR/data"

# --- download + build --------------------------------------------------------
log "Downloading $BUNDLE_URI"
rm -rf "$RELEASE_DIR" && mkdir -p "$RELEASE_DIR"
aws s3 cp "$BUNDLE_URI" "$RELEASE_DIR/bundle.tar.gz" --region "$AWS_REGION" --only-show-errors
tar -xzf "$RELEASE_DIR/bundle.tar.gz" -C "$RELEASE_DIR"
rm -f "$RELEASE_DIR/bundle.tar.gz"

# --- config.ini --------------------------------------------------------------
# Priority: config.ini in the bundle (hand-made bundles only; CodeBuild does not
# ship it, config.ini is not in git) > SSM parameter > existing $APP_DIR/config.ini.
if [ -s "$RELEASE_DIR/config.ini" ]; then
  cp "$RELEASE_DIR/config.ini" "$APP_DIR/config.ini"
  log "config.ini taken from release bundle"
elif aws ssm get-parameter --region "$AWS_REGION" --name "$CONFIG_PARAM" --with-decryption --query Parameter.Value --output text > "$APP_DIR/config.ini.new" 2>/dev/null; then
  mv "$APP_DIR/config.ini.new" "$APP_DIR/config.ini"
  log "config.ini refreshed from SSM parameter $CONFIG_PARAM"
else
  rm -f "$APP_DIR/config.ini.new"
  log "No config.ini in bundle and SSM parameter $CONFIG_PARAM not readable; using existing $APP_DIR/config.ini"
fi
[ -s "$APP_DIR/config.ini" ] || { log "ERROR: $APP_DIR/config.ini missing"; exit 1; }
chmod 640 "$APP_DIR/config.ini"
chown root:1000 "$APP_DIR/config.ini"

log "Building image $IMAGE"
docker build -t "$IMAGE" "$RELEASE_DIR"

PREV_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$APP_NAME" 2>/dev/null || true)"

run_container() {
  docker rm -f "$APP_NAME" >/dev/null 2>&1 || true
  docker run -d \
    --name "$APP_NAME" \
    --restart unless-stopped \
    -p "${BIND_ADDR}:${HOST_PORT}:8000" \
    -e ROOT_PATH="$ROOT_PATH" \
    -v "$APP_DIR/config.ini:/app/config.ini:ro" \
    -v "$APP_DIR/log:/app/log" \
    -v "$APP_DIR/data:/app/data" \
    --log-driver json-file --log-opt max-size=20m --log-opt max-file=5 \
    "$1" >/dev/null
}

healthy() {
  for _ in $(seq 1 45); do
    if curl -fsS "http://127.0.0.1:${HOST_PORT}/health" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

cleanup_old() {
  # keep the newest $KEEP_RELEASES release dirs and images
  ls -1t "$APP_DIR/releases" | tail -n +"$((KEEP_RELEASES + 1))" | while read -r old; do
    rm -rf "$APP_DIR/releases/$old"
    docker rmi "${APP_NAME}:${old}" >/dev/null 2>&1 || true
  done
  docker image prune -f >/dev/null 2>&1 || true
}

log "Starting $APP_NAME from $IMAGE on ${BIND_ADDR}:${HOST_PORT} (root path: ${ROOT_PATH:-/})"
run_container "$IMAGE"

if healthy; then
  log "Deployment healthy: $IMAGE"
  cleanup_old
  exit 0
fi

log "ERROR: health check failed. Container logs:"
docker logs --tail 80 "$APP_NAME" || true
if [ -n "$PREV_IMAGE" ] && [ "$PREV_IMAGE" != "$IMAGE" ]; then
  log "Rolling back to $PREV_IMAGE"
  run_container "$PREV_IMAGE"
  healthy && log "Rollback healthy" || log "ERROR: rollback also unhealthy"
fi
exit 1
