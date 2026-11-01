#!/bin/sh
# Explicit offline-update step for Ollama models. The private services have no egress
# to the internet (compose/docker-compose.yml, docs/security.md), so a model pull is
# not part of normal service operation: this script temporarily connects the running
# `ollama` container to the project's edge network, pulls the requested models, and
# disconnects it again. Embedding models are baked into the api/ingestion images at
# build time; updating them means rebuilding the images (make dev-up).
#
# Usage: scripts/model-update.sh [model ...]  (default: OLLAMA_MODEL from .env)
# The stack must be running (make dev-up) and the edge network is private to this
# command only for its duration. Also stocks the egress probe's image (busybox:1.36,
# pinned to pull_policy: never) so `make test-egress` needs no registry. Note: during
# the pull the ollama container *can* reach the internet; the run is only successful
# once the disconnect has succeeded (it is part of the update, not its cleanup after
# the fact). Run `make test-egress` afterwards to re-prove the egress rule.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$repo_dir"

env_file="$repo_dir/.env"
[ -f "$env_file" ] || env_file="$repo_dir/.env.example"

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

cid=$(docker compose --env-file "$env_file" -f compose/docker-compose.yml ps -q ollama)
[ -n "$cid" ] || fail "the ollama container is not running; start the stack with make dev-up"

edge_net=$(docker compose --env-file "$env_file" -f compose/docker-compose.yml config --format json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["networks"]["default"]["name"])')

if [ "$#" -gt 0 ]; then
    models="$*"
else
    models=$(docker compose --env-file "$env_file" -f compose/docker-compose.yml config --format json \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["ollama"]["environment"]["OLLAMA_MODEL"])')
fi
[ -n "$models" ] || fail "no model to pull"

echo "Connecting $cid to the edge network $edge_net for the update..."
docker network connect "$edge_net" "$cid"

# Backstop for failures during the update: restore the block before returning, and
# propagate the original error. A failed disconnect leaves the egress exception
# active, so it fails the run visibly with a recovery path rather than being a
# warning the update could still survive.
finish_update() {
    status=$?
    if docker network disconnect "$edge_net" "$cid"; then
        exit "$status"
    fi
    echo "FAIL: the ollama container ($cid) is still attached to the routable edge network $edge_net; the egress exception is still active." >&2
    echo "      recover:   docker network disconnect $edge_net $cid" >&2
    echo "      check:     docker inspect --format '{{json .NetworkSettings.Networks}}' $cid" >&2
    echo "      re-prove:  make test-egress" >&2
    exit 1
}
trap finish_update EXIT

for model in $models; do
    echo "Pulling $model..."
    docker exec "$cid" ollama pull "$model"
done

# Stock the egress probe's image too: it is pinned to pull_policy: never so the
# acceptance test runs network-free, and this offline-update step is the deliberate
# moment to fetch it. Cheap once present.
echo "Ensuring the egress probe image is on this host..."
docker pull busybox:1.36 >/dev/null

# Disconnecting is part of the update: the run only succeeds once the block is
# restored. On failure the EXIT trap retries and, if that fails too, exits 1 with
# the recovery path above. Once disconnected, drop the trap, so its retry cannot
# undo a successful cleanup.
echo "Disconnecting $cid from $edge_net..."
docker network disconnect "$edge_net" "$cid" >/dev/null
trap - EXIT

echo "Update finished; the container is disconnected again."
echo "Re-prove the egress rule: make test-egress"
