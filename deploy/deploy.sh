#!/usr/bin/env bash
# Back up the live SQLite database, build the current source, restart all five
# services, and prove the public site is serving this exact deployment.
set -euo pipefail

fail() {
    echo "DEPLOY FAILED: $*" >&2
    exit 1
}

# SQLite's backup API restarts the copy whenever the source is written, so a
# live collector/executor can starve it indefinitely on a busy database. Pause
# the writers for the copy only. resume_writers is idempotent and runs from an
# EXIT trap because --backup-only returns before the deploy would restart them.
WRITER_SERVICES="collector executor priors shadow"
PAUSED_WRITERS=""

resume_writers() {
    [ -n "$PAUSED_WRITERS" ] || return 0
    local services="$PAUSED_WRITERS"
    PAUSED_WRITERS=""
    echo "resuming $services"
    # shellcheck disable=SC2086
    docker compose start $services >/dev/null 2>&1 || \
        echo "WARNING: could not resume $services; start them manually" >&2
}

pause_writers() {
    local service container running
    PAUSED_WRITERS=""
    for service in $WRITER_SERVICES; do
        container="$(docker compose ps -q "$service" 2>/dev/null || true)"
        [ -n "$container" ] || continue
        running="$(docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null || true)"
        [ "$running" = "true" ] || continue
        PAUSED_WRITERS="${PAUSED_WRITERS:+$PAUSED_WRITERS }$service"
    done
    [ -n "$PAUSED_WRITERS" ] || return 0
    echo "pausing $PAUSED_WRITERS so the backup cannot be starved"
    # shellcheck disable=SC2086
    if ! docker compose stop $PAUSED_WRITERS >/dev/null 2>&1; then
        resume_writers
        fail "could not pause the writers before the backup"
    fi
}

if [ -n "${PROJECT_ROOT:-}" ]; then
    PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd -P)"
else
    PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fi
cd "$PROJECT_ROOT"

DATABASE_PATH="${DATABASE_PATH:-$PROJECT_ROOT/data/esports_live.sqlite3}"
BACKUP_DIR="${BACKUP_DIR:-$PROJECT_ROOT/data/backups}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://esports.zhng.tech}"
DEPLOY_GIT_SHA="${DEPLOY_GIT_SHA:-unknown}"
ASSETS=(index.html app.js app.css detail.html detail.js)
SERVICES=(collector executor priors shadow dashboard)

MODE=deploy
PREDEPLOY_BACKUP=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --backup-only)
            MODE=backup-only
            shift
            ;;
        --pre-deploy-backup)
            [ "$#" -ge 2 ] || fail "--pre-deploy-backup requires a path"
            PREDEPLOY_BACKUP="$2"
            shift 2
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

integrity_check() {
    local database="$1"
    local result
    if ! result="$(sqlite3 -readonly "$database" "PRAGMA integrity_check;")"; then
        return 1
    fi
    [ "$result" = "ok" ]
}

create_backup() {
    local timestamp backup_path
    command -v sqlite3 >/dev/null 2>&1 || fail "sqlite3 is required for the online backup"
    [ -f "$DATABASE_PATH" ] || fail "production database is missing: $DATABASE_PATH"
    mkdir -p "$BACKUP_DIR"
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    backup_path="$BACKUP_DIR/esports_live.${timestamp}.pre-deploy.sqlite3"
    [ ! -e "$backup_path" ] || fail "refusing to overwrite existing backup: $backup_path"

    echo "creating SQLite online backup $backup_path"
    umask 077
    pause_writers
    if ! sqlite3 "$DATABASE_PATH" ".timeout 30000" ".backup '$backup_path'"; then
        resume_writers
        rm -f -- "$backup_path"
        fail "SQLite .backup failed; deployment has not started"
    fi
    resume_writers
    if ! integrity_check "$backup_path"; then
        rm -f -- "$backup_path"
        fail "backup integrity_check did not return ok; deployment has not started"
    fi
    echo "backup integrity_check ok"
    echo "PRE_DEPLOY_BACKUP=$backup_path"
}

verify_existing_backup() {
    local backup_path="$1"
    local expected_dir actual_dir backup_name
    [ -f "$backup_path" ] || fail "pre-deploy backup is missing: $backup_path"
    [ ! -L "$backup_path" ] || fail "pre-deploy backup must not be a symlink"
    expected_dir="$(cd "$BACKUP_DIR" && pwd -P)"
    actual_dir="$(cd "$(dirname "$backup_path")" && pwd -P)"
    [ "$actual_dir" = "$expected_dir" ] || fail "pre-deploy backup is outside $BACKUP_DIR"
    backup_name="$(basename "$backup_path")"
    if ! printf '%s\n' "$backup_name" | grep -Eq \
        '^esports_live\.[0-9]{8}T[0-9]{6}Z\.pre-deploy\.sqlite3$'; then
        fail "pre-deploy backup has an unexpected name: $backup_name"
    fi
    integrity_check "$backup_path" || fail "pre-deploy backup no longer passes integrity_check"
    echo "using verified pre-deploy backup $backup_path"
    echo "PRE_DEPLOY_BACKUP=$backup_path"
}

trap resume_writers EXIT

if [ -n "$PREDEPLOY_BACKUP" ]; then
    verify_existing_backup "$PREDEPLOY_BACKUP"
else
    create_backup
fi

if [ "$MODE" = "backup-only" ]; then
    exit 0
fi

# Version 0.6 starts a clean execution-paper-v2 cohort because its planner
# suppresses risk-exhausted and dust orders differently. Do not strand risk in
# the old cohort when collector/executor/dashboard switch accounts together.
LEGACY_OPEN_POSITIONS="$(sqlite3 -readonly "$DATABASE_PATH" \
    "SELECT count(*) FROM paper_positions p JOIN paper_accounts a USING(account_id) WHERE a.name='execution-paper' AND p.shares>0.000000001;")"
LEGACY_ACTIVE_ORDERS="$(sqlite3 -readonly "$DATABASE_PATH" \
    "SELECT count(*) FROM paper_orders o JOIN paper_accounts a USING(account_id) WHERE a.name='execution-paper' AND o.status IN ('PENDING','SUBMITTED');")"
if [ "$LEGACY_OPEN_POSITIONS" -ne 0 ] || [ "$LEGACY_ACTIVE_ORDERS" -ne 0 ]; then
    fail "execution-paper still has $LEGACY_OPEN_POSITIONS open positions and $LEGACY_ACTIVE_ORDERS active orders; wait for a clean v2 cohort cutover"
fi
echo "legacy execution-paper cohort is flat; v2 cutover is safe"

TAG="$(grep -m1 -oE 'polytrade-esports-live:[0-9.]+' compose.yaml || true)"
[ -n "$TAG" ] || fail "could not read the image tag from compose.yaml"
if ! printf '%s\n' "$DEPLOY_GIT_SHA" | grep -Eq '^([0-9a-f]{40}|unknown)$'; then
    fail "DEPLOY_GIT_SHA must be the 40-character GitHub commit SHA"
fi
# Bytecode is a build artifact, not source. Including it would make the same
# source hash differently depending on whether anyone ran Python in the tree.
SOURCE_SHA="$(find src -type f ! -name '*.py[cod]' -not -path '*/__pycache__/*' \
    -exec sha256sum {} + | sort -k2 | sha256sum | cut -c1-12)"
echo "building $TAG from Git commit $DEPLOY_GIT_SHA and source $SOURCE_SHA"
if ! docker build \
    --build-arg "SOURCE_SHA=$SOURCE_SHA" \
    --build-arg "GIT_SHA=$DEPLOY_GIT_SHA" \
    -t "$TAG" .; then
    fail "Docker image build failed"
fi

echo "recreating collector, executor, priors, shadow, and dashboard"
if ! docker compose up -d --force-recreate; then
    fail "docker compose could not recreate the production services"
fi

echo "waiting for the dashboard health check"
READY=0
for _ in $(seq 1 60); do
    if [ "$(curl -fsS "http://127.0.0.1:8788/healthz" 2>/dev/null || true)" = "ok" ]; then
        READY=1
        break
    fi
    sleep 2
done
[ "$READY" -eq 1 ] || fail "dashboard /healthz did not return ok"

for SERVICE in "${SERVICES[@]}"; do
    CONTAINER_ID="$(docker compose ps -q "$SERVICE")"
    [ -n "$CONTAINER_ID" ] || fail "$SERVICE container is missing or stopped"
    RUNNING="$(docker inspect --format '{{.State.Running}}' "$CONTAINER_ID")"
    [ "$RUNNING" = "true" ] || fail "$SERVICE container is not running"
    CONTAINER_SOURCE_SHA="$(docker exec "$CONTAINER_ID" cat /app/.source-sha)"
    [ "$CONTAINER_SOURCE_SHA" = "$SOURCE_SHA" ] || \
        fail "$SERVICE source $CONTAINER_SOURCE_SHA != deployed source $SOURCE_SHA"
    CONTAINER_GIT_SHA="$(docker exec "$CONTAINER_ID" cat /app/.git-sha)"
    [ "$CONTAINER_GIT_SHA" = "$DEPLOY_GIT_SHA" ] || \
        fail "$SERVICE Git SHA $CONTAINER_GIT_SHA != deployment $DEPLOY_GIT_SHA"
    echo "$SERVICE running: git=$CONTAINER_GIT_SHA source=$CONTAINER_SOURCE_SHA"
done

DASHBOARD_ID="$(docker compose ps -q dashboard)"
DASHBOARD_HEALTH="starting"
for _ in $(seq 1 60); do
    DASHBOARD_HEALTH="$(docker inspect \
        --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
        "$DASHBOARD_ID")"
    [ "$DASHBOARD_HEALTH" = "healthy" ] && break
    sleep 2
done
[ "$DASHBOARD_HEALTH" = "healthy" ] || fail "dashboard container health is $DASHBOARD_HEALTH"

for ASSET in "${ASSETS[@]}"; do
    CONTAINER_SHA="$(docker exec "$DASHBOARD_ID" \
        sha256sum "/app/src/polytrade_esports/web/$ASSET" | cut -d' ' -f1)"
    LOCAL_SHA="$(sha256sum "src/polytrade_esports/web/$ASSET" | cut -d' ' -f1)"
    echo "asset $ASSET container=$CONTAINER_SHA local=$LOCAL_SHA"
    [ "$CONTAINER_SHA" = "$LOCAL_SHA" ] || \
        fail "dashboard container is not serving the current $ASSET"
done

ROOT_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8788/)"
[ "$ROOT_STATUS" = "401" ] || fail "dashboard root returned $ROOT_STATUS instead of 401"

integrity_check "$DATABASE_PATH" || fail "production database integrity_check did not return ok"
SCHEMA_VERSION="$(sqlite3 -readonly "$DATABASE_PATH" \
    "SELECT value FROM metadata WHERE key='schema_version';")"
[ "$SCHEMA_VERSION" = "8" ] || fail "metadata schema_version is $SCHEMA_VERSION instead of 8"
ACCOUNT_COUNT="$(sqlite3 -readonly "$DATABASE_PATH" \
    "SELECT count(*) FROM paper_accounts WHERE name IN ('live-paper','grounded-paper','execution-paper','execution-paper-v2');")"
[ "$ACCOUNT_COUNT" = "4" ] || fail "one or more protected paper accounts are missing"

EXECUTOR_READY=0
EXECUTOR_STATE="missing"
for _ in $(seq 1 30); do
    EXECUTOR_STATE="$(sqlite3 -readonly "$DATABASE_PATH" \
        "SELECT status || '|' || coalesce(last_heartbeat_at,'') || '|' || coalesce(CAST((julianday('now')-julianday(last_heartbeat_at))*86400 AS INTEGER),999999) FROM executor_status WHERE singleton=1;" \
        2>/dev/null || true)"
    EXECUTOR_STATUS="${EXECUTOR_STATE%%|*}"
    HEARTBEAT_AGE="${EXECUTOR_STATE##*|}"
    if [ "$EXECUTOR_STATUS" = "running" ] && \
        printf '%s\n' "$HEARTBEAT_AGE" | grep -Eq '^-?[0-9]+$' && \
        [ "$HEARTBEAT_AGE" -ge -5 ] && [ "$HEARTBEAT_AGE" -le 20 ]; then
        EXECUTOR_READY=1
        break
    fi
    sleep 1
done
[ "$EXECUTOR_READY" -eq 1 ] || \
    fail "executor status/heartbeat is not fresh: $EXECUTOR_STATE"
echo "database integrity=ok schema=8 executor=$EXECUTOR_STATE"

ERROR_PATTERN='Traceback|Exception|FATAL|PANIC|(^|[[:space:]])ERROR([[:space:]:]|$)'
for SERVICE in "${SERVICES[@]}"; do
    RECENT_LOGS="$(docker compose logs --since 10m --no-color "$SERVICE" 2>&1)"
    if printf '%s\n' "$RECENT_LOGS" | grep -Eq "$ERROR_PATTERN"; then
        printf '%s\n' "$RECENT_LOGS" >&2
        fail "$SERVICE logs contain an error in the last 10 minutes"
    fi
    echo "$SERVICE recent logs: no error signatures"
done

PUBLIC_HEALTH=""
for _ in $(seq 1 15); do
    PUBLIC_HEALTH="$(curl -fsS "$PUBLIC_BASE_URL/healthz" 2>/dev/null || true)"
    [ "$PUBLIC_HEALTH" = "ok" ] && break
    sleep 2
done
[ "$PUBLIC_HEALTH" = "ok" ] || fail "public $PUBLIC_BASE_URL/healthz did not return ok"

PUBLIC_MANIFEST="$(curl -fsS "$PUBLIC_BASE_URL/healthz/assets")" || \
    fail "could not fetch the public asset manifest"
if ! MANIFEST_JSON="$PUBLIC_MANIFEST" PROJECT_ROOT="$PROJECT_ROOT" python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

assets = ("index.html", "app.js", "app.css", "detail.html", "detail.js")
payload = json.loads(os.environ["MANIFEST_JSON"])
expected = {
    name: hashlib.sha256(
        (Path(os.environ["PROJECT_ROOT"]) / "src/polytrade_esports/web" / name).read_bytes()
    ).hexdigest()
    for name in assets
}
if payload != {"status": "ok", "assets": expected}:
    raise SystemExit("public asset manifest does not match the deployed source")
PY
then
    fail "public site is not serving the current five frontend assets"
fi

PUBLIC_ROOT_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' "$PUBLIC_BASE_URL/")"
[ "$PUBLIC_ROOT_STATUS" = "401" ] || \
    fail "public dashboard root returned $PUBLIC_ROOT_STATUS instead of 401"

docker compose ps --format "table {{.Name}}\t{{.Status}}"
echo "DEPLOYED_IMAGE=$TAG"
echo "DEPLOYED_GIT_SHA=$DEPLOY_GIT_SHA"
echo "DEPLOYED_SOURCE_SHA=$SOURCE_SHA"
echo "PUBLIC_HEALTH=ok"
echo "deploy verified"
