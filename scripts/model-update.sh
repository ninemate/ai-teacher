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

# TEACHER_ENV_FILE points at another stack's settings (scripts/e2e.sh).
env_file="${TEACHER_ENV_FILE:-$repo_dir/.env}"
[ -f "$env_file" ] || env_file="$repo_dir/.env.example"

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

cid=$(docker compose --env-file "$env_file" -f compose/docker-compose.yml ps -q ollama)
[ -n "$cid" ] || fail "the ollama container is not running; start the stack with make dev-up"

# Merged with the proxy overlay: compose leaves a network no service uses out of the config, and only the proxy
# is on the edge network.
merged_config=$(docker compose --env-file "$env_file" -f compose/docker-compose.yml \
    -f compose/docker-compose.dev.yml -f compose/docker-compose.proxy.yml config --format json)
edge_net=$(printf '%s' "$merged_config" | python3 -c 'import json,sys; print(json.load(sys.stdin)["networks"]["default"]["name"])')

if [ "$#" -gt 0 ]; then
    models="$*"
else
    # The api is the service that uses the model; the ollama container itself is not told its name.
    models=$(printf '%s' "$merged_config" | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["api"]["environment"]["OLLAMA_MODEL"])')
fi
[ -n "$models" ] || fail "no model to pull"

echo "Creating a pre-update backup before any model changes..."
backup_output=$(TEACHER_ENV_FILE="$env_file" BACKUP_QDRANT_TRANSPORT=compose python3 "$script_dir/backup_restore.py" backup) \
    || fail "pre-update backup failed; no model was changed"
printf '%s\n' "$backup_output"
backup_path=${backup_output#Backup created: }

compose_api() {
    docker compose --env-file "$env_file" -f compose/docker-compose.yml \
        -f compose/docker-compose.dev.yml -f compose/docker-compose.proxy.yml \
        exec -T api python -c "$1" "$2"
}

model_metadata='import json,os,sys,urllib.request; name=sys.argv[1]; rows=json.load(urllib.request.urlopen(os.environ["OLLAMA_BASE_URL"].rstrip("/")+"/api/tags"))["models"]; key=name if ":" in name else name+":latest"; row=next((m for m in rows if m["name"] == key), None); print("\t".join((row["name"],row["digest"],row["modified_at"])) if row else "")'
model_digest() {
    compose_api "$model_metadata" "$1"
}

update_stamp=$(date -u +%Y%m%dT%H%M%SZ)
data_dir=$(python3 - "$env_file" <<'PY'
from pathlib import Path
import sys

for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("TEACHER_DATA_DIR="):
        print(line.split("=", 1)[1].strip().strip("\"'"))
        break
else:
    print("/srv/teacher-agent")
PY
)
model_updates_file=$data_dir/components/model-update-records.json
mkdir -p "$(dirname "$model_updates_file")"
chmod 700 "$(dirname "$model_updates_file")"

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
    previous=$(model_digest "$model")
    rollback_tag="not previously installed"
    if [ -n "$previous" ]; then
        previous_name=${previous%%$(printf '\t')*}
        previous_rest=${previous#*$(printf '\t')}
        previous_digest=${previous_rest%%$(printf '\t')*}
        short_digest=$(printf '%s' "$previous_digest" | sed 's/^sha256://' | cut -c1-12)
        model_base=${previous_name%:*}
        rollback_tag="$model_base:rollback-$update_stamp-$short_digest"
        echo "Preserving current $previous_name ($previous_digest) as $rollback_tag..."
        docker exec "$cid" ollama cp "$previous_name" "$rollback_tag"
    fi
    echo "Pulling $model..."
    docker exec "$cid" ollama pull "$model"
    installed=$(model_digest "$model")
    [ -n "$installed" ] || fail "could not read the installed digest for $model; restore from $rollback_tag"
    installed_name=${installed%%$(printf '\t')*}
    installed_rest=${installed#*$(printf '\t')}
    installed_digest=${installed_rest%%$(printf '\t')*}
    installed_date=${installed_rest#*$(printf '\t')}
    case "$model" in
        qwen2.5:7b-instruct-q4_K_M) model_source=https://ollama.com/library/qwen2.5; model_license=Apache-2.0 ;;
        gemma2:2b) model_source=https://ollama.com/library/gemma2; model_license="Gemma Terms of Use" ;;
        *) model_source="not checked"; model_license="not checked" ;;
    esac
    MODEL_UPDATE_NAME="$installed_name" MODEL_UPDATE_DIGEST="$installed_digest" \
        MODEL_UPDATE_DATE="$installed_date" MODEL_UPDATE_SOURCE="$model_source" \
        MODEL_UPDATE_LICENSE="$model_license" MODEL_UPDATE_ROLLBACK="$rollback_tag" \
        MODEL_UPDATE_BACKUP="$backup_path" MODEL_UPDATE_OLLAMA_VERSION="$(docker exec "$cid" ollama --version | head -n 1)" \
        python3 - "$model_updates_file" <<'PY'
import json, os, sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    records = json.loads(path.read_text(encoding="utf-8"))
except FileNotFoundError:
    records = []
records.append({
    "model": os.environ["MODEL_UPDATE_NAME"],
    "version": os.environ["MODEL_UPDATE_NAME"],
    "license": os.environ["MODEL_UPDATE_LICENSE"],
    "source": os.environ["MODEL_UPDATE_SOURCE"],
    "checksum": os.environ["MODEL_UPDATE_DIGEST"],
    "dependencies": ["Ollama " + os.environ["MODEL_UPDATE_OLLAMA_VERSION"]],
    "install_date": os.environ["MODEL_UPDATE_DATE"],
    "rollback_path": os.environ["MODEL_UPDATE_ROLLBACK"] + "; state backup " + os.environ["MODEL_UPDATE_BACKUP"],
})
path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
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
