#!/bin/sh
# Check that the library share is usable before (or after) starting the stack:
# the mount point is a real mount, is readable, has its sentinel file (if configured) and contains
# every watched folder from the ingestion config. Run on the host that mounts the share.
#
#   scripts/check-library.sh [mount-point]
#
# Reads TEACHER_MOUNT_POINT, LIBRARY_SENTINEL and INGEST_CONFIG_PATH from the environment, falling back to
# .env and config/ingestion/ingestion.yml. REQUIRE_MOUNTPOINT=0 accepts a plain local folder (dev).
# Exits 0 when everything checks out, 1 otherwise.
set -u

env_value() { # name: value from the environment, else from .env (without sourcing it)
  eval "current=\${$1:-}"
  if [ -n "$current" ]; then printf '%s' "$current"; return; fi
  [ -f .env ] && sed -n "s/^$1=//p" .env | tail -n 1 | sed "s/^['\"]//; s/['\"]\$//"
}

mount_point="${1:-$(env_value TEACHER_MOUNT_POINT)}"
sentinel="$(env_value LIBRARY_SENTINEL)"
config="$(env_value INGEST_CONFIG_PATH)"
case "$config" in ""|/app/*) config="config/ingestion/ingestion.yml" ;; esac
failures=0

fail() { printf 'FAIL  %s\n' "$1"; failures=$((failures + 1)); }
ok() { printf 'ok    %s\n' "$1"; }

if [ -z "$mount_point" ]; then
  echo "FAIL  no mount point: set TEACHER_MOUNT_POINT or pass it as an argument"; exit 1
fi

if [ ! -d "$mount_point" ]; then
  fail "$mount_point is not a directory"
  exit 1
fi

if [ "${REQUIRE_MOUNTPOINT:-1}" = "0" ]; then
  ok "mount point check skipped (REQUIRE_MOUNTPOINT=0)"
elif command -v mountpoint >/dev/null 2>&1; then
  # Reading the directory first triggers a systemd automount, which only mounts on access.
  ls "$mount_point" >/dev/null 2>&1
  if mountpoint -q "$mount_point"; then ok "$mount_point is a mount point"; else fail "$mount_point is not a mount point (share not mounted? it would look like an empty folder)"; fi
else
  fail "mountpoint(1) not found; install util-linux or set REQUIRE_MOUNTPOINT=0"
fi

if listing="$(ls -A "$mount_point" 2>&1)"; then
  ok "$mount_point is readable"
  [ -n "$listing" ] || fail "$mount_point is empty"
else
  fail "$mount_point is not readable: $listing"
fi

if [ -n "$sentinel" ]; then
  if [ -e "$mount_point/$sentinel" ]; then ok "sentinel $sentinel present"; else fail "sentinel $sentinel missing in $mount_point"; fi
fi

if [ -f "$config" ]; then
  folders="$(sed -n 's/^[[:space:]-]*path:[[:space:]]*//p' "$config" | sed "s/[[:space:]]*#.*//; s/^['\"]//; s/['\"]\$//")"
  if [ -z "$folders" ]; then
    echo "warn  no watched folders in $config; nothing will be indexed"
  else
    old_ifs="$IFS"
    IFS='
'
    for folder in $folders; do
      if [ -d "$mount_point/$folder" ]; then
        count="$(find "$mount_point/$folder" -type f 2>/dev/null | head -n 1000 | wc -l | tr -d ' ')"
        ok "watched folder $folder ($count files, counted up to 1000)"
      else
        fail "watched folder $folder missing in $mount_point"
      fi
    done
    IFS="$old_ifs"
  fi
else
  echo "warn  no ingestion config at $config; watched folders not checked"
fi

if [ "$failures" -gt 0 ]; then echo "Library check failed ($failures problem(s))."; exit 1; fi
echo "Library check passed."
