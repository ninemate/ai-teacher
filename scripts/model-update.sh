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
# the pull the ollama container *can* reach the internet; the block resumes when the
# command finishes. Run `make test-egress` afterwards to re-prove the egress rule.
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
finish() {
    docker network disconnect "$edge_net" "$cid" >/dev/null 2>&1 || \
        echo "warning: could not disconnect from $edge_net; check: docker inspect $cid" >&2
}
trap finish EXIT

for model in $models; do
    echo "Pulling $model..."
    docker exec "$cid" ollama pull "$model"
done

# Stock the egress probe's image too: it is pinned to pull_policy: never so the
# acceptance test runs network-free, and this offline-update step is the deliberate
# moment to fetch it. Cheap once present.
echo "Ensuring the egress probe image is on this host..."
docker pull busybox:1.36 >/dev/null

echo "Update finished; the container is disconnected again."
echo "Re-prove the egress rule: make test-egress"
