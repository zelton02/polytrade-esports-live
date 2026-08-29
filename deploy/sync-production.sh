#!/usr/bin/env bash
# Synchronize a clean checkout while proving protected production state cannot
# be overwritten or deleted. The dry run is a required safety gate.
set -euo pipefail

fail() {
    echo "SYNC FAILED: $*" >&2
    exit 1
}

[ "$#" -eq 2 ] || fail "usage: sync-production.sh SOURCE DESTINATION"
SOURCE_ROOT="$(cd "$1" && pwd -P)"
DESTINATION="$2"

case "$DESTINATION" in
    ""|/|.|..|*:|*/:|*:/|*:~|*:~/)
        fail "refusing unsafe destination: $DESTINATION"
        ;;
esac

RSYNC_ARGS=(
    -az
    --itemize-changes
    --delete-delay
    --filter='protect /.env'
    --filter='protect /data/***'
    --filter='protect backups/***'
    --filter='protect *.sqlite3'
    --filter='protect *.sqlite3-*'
    --exclude='/.git/'
    --exclude='/.env'
    --exclude='/data/'
    --exclude='/.DS_Store'
    --exclude='*.sqlite3'
    --exclude='*.sqlite3-*'
    --exclude='backups/'
)

echo "verified rsync dry run"
if ! PLAN="$(rsync --dry-run "${RSYNC_ARGS[@]}" -- "$SOURCE_ROOT/" "$DESTINATION/")"; then
    fail "rsync dry run failed"
fi
printf '%s\n' "$PLAN"

# Excludes already protect these paths. This second guard makes a future filter
# regression fail before the mutating rsync command is ever reached.
if printf '%s\n' "$PLAN" | grep -Eq \
    '^\*deleting[[:space:]]+((\.env)(/|$)|data(/|$)|.*backups(/|$)|.*\.sqlite3(-[^/]*)?$)'; then
    fail "dry run attempted to delete protected production state"
fi

if ! rsync "${RSYNC_ARGS[@]}" -- "$SOURCE_ROOT/" "$DESTINATION/"; then
    fail "production source synchronization failed"
fi

# A second dry run must contain no file changes. A root-directory timestamp is
# harmless and can vary on older rsync versions, so it is the only ignored row.
if ! REMAINING="$(rsync --dry-run "${RSYNC_ARGS[@]}" -- "$SOURCE_ROOT/" "$DESTINATION/")"; then
    fail "post-sync verification dry run failed"
fi
REMAINING="$(printf '%s\n' "$REMAINING" | grep -Ev '^[.]d.*[[:space:]]+\./$' || true)"
[ -z "$REMAINING" ] || {
    printf '%s\n' "$REMAINING" >&2
    fail "production source still differs after synchronization"
}
echo "production source synchronization verified"
