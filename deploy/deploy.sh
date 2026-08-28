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
for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null "http://127.0.0.1:8788/healthz"; then break; fi
    sleep 2
done

# The served bytes are the only proof that matters; the image can be correct
# while a stale container is still bound to the port.
# The credential goes in on stdin as a curl config file rather than on the
# command line, where it would be readable in `ps` by any user on the host.
SERVED_SHA="$(printf 'user = "%s"\n' "${DASH_AUTH:-}" \
    | curl -fsS -K - http://127.0.0.1:8788/app.css | sha256sum | cut -c1-12)"
LOCAL_SHA="$(sha256sum src/polytrade_esports/web/app.css | cut -c1-12)"
echo "served app.css $SERVED_SHA  local $LOCAL_SHA"
if [ "$SERVED_SHA" != "$LOCAL_SHA" ]; then
    echo "DEPLOY FAILED: the container is not serving the current app.css" >&2
    exit 1
fi
docker compose ps --format "table {{.Name}}\t{{.Status}}"
echo "deploy verified"
