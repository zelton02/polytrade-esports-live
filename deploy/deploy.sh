#!/usr/bin/env bash
# Build and restart the stack, then prove the new code is actually serving.
#
# Why this exists rather than `docker compose up -d --build`: on this host
# `docker compose build` exits 0 and does nothing (no buildx plugin), so a
# deploy reports success while the containers keep running the old image. That
# failed silently twice. `docker build` works, so the tag is built directly and
# then verified over HTTP.
set -euo pipefail

cd "$(dirname "$0")/.."

TAG="$(grep -m1 -oE 'polytrade-esports-live:[0-9.]+' compose.yaml)"
if [ -z "$TAG" ]; then
    echo "could not read the image tag from compose.yaml" >&2
    exit 1
fi
echo "building $TAG"
docker build -t "$TAG" .

echo "recreating containers"
docker compose up -d --force-recreate

echo "waiting for the dashboard"
READY=0
for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null "http://127.0.0.1:8788/healthz"; then
        READY=1
        break
    fi
    sleep 2
done
if [ "$READY" -ne 1 ]; then
    echo "DEPLOY FAILED: dashboard health check did not become ready" >&2
    exit 1
fi

# Production stores only the dashboard password hash, not a plaintext Basic
# Auth credential. Verify the file inside the running container and prove the
# bound HTTP port is both healthy and protected.
DASHBOARD_ID="$(docker compose ps -q dashboard)"
if [ -z "$DASHBOARD_ID" ]; then
    echo "DEPLOY FAILED: dashboard container is missing" >&2
    exit 1
fi
CONTAINER_SHA="$(docker exec "$DASHBOARD_ID" \
    sha256sum /app/src/polytrade_esports/web/app.css | cut -c1-12)"
LOCAL_SHA="$(sha256sum src/polytrade_esports/web/app.css | cut -c1-12)"
echo "container app.css $CONTAINER_SHA  local $LOCAL_SHA"
if [ "$CONTAINER_SHA" != "$LOCAL_SHA" ]; then
    echo "DEPLOY FAILED: the container is not serving the current app.css" >&2
    exit 1
fi
ROOT_STATUS="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8788/)"
if [ "$ROOT_STATUS" != "401" ]; then
    echo "DEPLOY FAILED: dashboard root returned $ROOT_STATUS instead of 401" >&2
    exit 1
fi
docker compose ps --format "table {{.Name}}\t{{.Status}}"
echo "deploy verified"
