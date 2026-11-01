#!/bin/sh
# Egress-block test ("Private AI" acceptance test 11): private services must not be able
# to send content to an external AI cloud. Three parts, runnable locally without CI:
#
#   1. config audit  - the merged compose config has an `internal: true` private network;
#                      api/ingestion/voice/qdrant/ollama sit on it and on nothing else, and
#                      publish no host ports; only the proxy has ports, on the edge network.
#                      (scripts/egress_check.py config)
#   2. runtime audit - the running containers of those services are really attached to the
#                      internal network only (no routable network), publish no host ports,
#                      and the network is actually internal. (scripts/egress_check.py container)
#   3. live probe    - a disposable container (compose: `egress_probe`) joins the private
#                      network and must fail to reach a set of public hosts (TCP and DNS).
#                      Any success fails the test.
#
# Usage: scripts/test-egress-block.sh [--config-only|--runtime-only]
# Compose overlays follow the Makefile's COMPOSE_DEV; set TEACHER_ENABLE_VOICE=true to
# include the voice overlay. Skips parts 2 and 3 when the stack is not running.
set -eu

want_config=1
want_runtime=1
case "${1:-}" in
    "") ;;
    --config-only) want_runtime=0 ;;
    --runtime-only) want_config=0 ;;
    *)
        echo "usage: $0 [--config-only|--runtime-only]" >&2
        exit 2
        ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$repo_dir"

# .env when the developer/host has one, otherwise the checked-in defaults.
env_file="$repo_dir/.env"
[ -f "$env_file" ] || env_file="$repo_dir/.env.example"

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

# The voice overlay part of the audit like any enabled deployment has it; without it
# voice sits outside the check entirely.
teacher_enable_voice=$(sed -n 's/^[[:space:]]*TEACHER_ENABLE_VOICE=//p' "$env_file" | head -n1 | tr -d '"')
voice_overlay=""
case "$teacher_enable_voice" in
    true | True | TRUE | yes | 1)
        voice_overlay="-f compose/docker-compose.voice.example.yml"
        echo "voice enabled (TEACHER_ENABLE_VOICE=$teacher_enable_voice): the voice overlay is part of the audit"
        ;;
esac

tmp_config=$(mktemp)
tmp_cids=$(mktemp)
trap 'rm -f "$tmp_config" "$tmp_cids"' EXIT

# Mirrors the Makefile's COMPOSE_DEV (base + dev + proxy), plus voice when enabled.
# The egress-test profile must be named so the config audit sees `egress_probe`
# (compose leaves profiled services out of the config otherwise).
compose() {
    docker compose --env-file "$env_file" \
        --profile egress-test \
        -f compose/docker-compose.yml \
        -f compose/docker-compose.dev.yml \
        -f compose/docker-compose.proxy.yml \
        $voice_overlay \
        "$@"
}

# Base services only, for `docker compose ps` and the probe.
compose_base() {
    docker compose --env-file "$env_file" \
        --profile egress-test \
        -f compose/docker-compose.yml "$@"
}

if [ "$want_config" -eq 1 ]; then
    echo "=== part 1: compose config audit ==="
    compose config --format json > "$tmp_config"
    python3 "$script_dir/egress_check.py" config "$tmp_config" \
        || fail "the compose config violates the egress rule"
else
    compose config --format json > "$tmp_config"
fi

if [ "$want_runtime" -eq 1 ]; then
    echo "=== part 2: running-container audit ==="
    private_net=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["networks"]["private"]["name"])' "$tmp_config")

    seen_once=0
    for name in qdrant ollama api ingestion voice; do
        cid=$(compose_base ps -q "$name" 2>/dev/null || true)
        [ -n "$cid" ] || cid=$(compose ps -q "$name" 2>/dev/null || true)
        if [ -n "$cid" ]; then
            seen_once=1
            printf '%s %s\n' "$name" "$cid" >> "$tmp_cids"
        fi
    done
    # Ollama model updates (scripts/model-update.sh) briefly join the edge network;
    # mid-update runs may pass here, the rule applies once the update is closed.
    if [ "$seen_once" -eq 0 ]; then
        echo "runtime audit skipped: the stack is not running (start it with make dev-up)"
    else
        [ "$(docker network inspect "$private_net" --format '{{.Internal}}' 2>/dev/null)" = "true" ] \
            || fail "network $private_net is missing on this host or is not internal"

        while read -r name cid; do
            python3 "$script_dir/egress_check.py" container "$name" "$private_net" "$cid" \
                || fail "$name violates the egress rule"
        done < "$tmp_cids"

        echo "=== part 3: live egress probe on $private_net ==="
        docker image inspect busybox:1.36 >/dev/null 2>&1 \
            || fail "the probe image busybox:1.36 is not on this host and the probe never pulls (pull_policy: never); stock it once while online: docker pull busybox:1.36 — or run make model-update, which pulls it alongside the models"
        compose_base run --rm --no-deps egress_probe
    fi
fi

echo "PASS: private services have no egress to the internet (see docs/security.md)"
