#!/bin/bash
# End-to-end trial of the whole stack (`make test-e2e`): brings up its own isolated copy of the dev stack, with a small
# chat model and Hungarian fixtures (tests/e2e/fixtures), and runs scripts/e2e_trial.py against the HTTPS proxy.
# Project name, ports, library and data are the trial's own, so a dev stack on the same host is left alone. Each run
# starts from an empty index and database; the pulled model is kept for the next run.
#
# Usage: scripts/e2e.sh [--native] [--voice] [--keep]
#   --native  no containers: scripts/e2e-native.sh, for hosts where Docker cannot run. Proves neither images nor network rule.
#   --voice   also build the voice overlay and do a Hungarian TTS -> STT round trip (large download on first build).
#   --keep    leave the stack running afterwards.
# Settings: E2E_OLLAMA_MODEL (small chat model), E2E_WORK (working folder), E2E_MODEL_TIMEOUT (seconds per LLM call).
set -eu

backend=compose
voice=0
keep=0
for arg in "$@"; do
    case "$arg" in
        --native) backend=native ;;
        --voice) voice=1 ;;
        --keep) keep=1 ;;
        *) echo "usage: $0 [--native] [--voice] [--keep]" >&2; exit 2 ;;
    esac
done

repo=$(CDPATH="" cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"
work=${E2E_WORK:-$repo/.scratch/e2e}
model=${E2E_OLLAMA_MODEL:-qwen2.5:0.5b}
export TEACHER_HTTPS_PORT=${TEACHER_HTTPS_PORT:-18443}
export TEACHER_PROXY_HTTP_PORT=${TEACHER_PROXY_HTTP_PORT:-18088}
mkdir -p "$work"

random_secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }
ANNA_PASSWORD=$(random_secret)
BELA_PASSWORD=$(random_secret)
CILI_PASSWORD=$(random_secret)
ADMIN_PASSWORD=$(random_secret)
INGEST_TOKEN=$(random_secret)
export ANNA_PASSWORD BELA_PASSWORD CILI_PASSWORD ADMIN_PASSWORD

# Prints TEACHER_AUTH_USERS for the trial's accounts.
# shellcheck disable=SC2016
hash_users_py='
import os
from argon2 import PasswordHasher
hasher = PasswordHasher()
print(";".join(f"{name}:{hasher.hash(os.environ[var])}" for name, var in (
    ("e2e-anna", "ANNA_PASSWORD"), ("e2e-bela", "BELA_PASSWORD"), ("e2e-cili", "CILI_PASSWORD"), ("e2e-admin", "ADMIN_PASSWORD"))))
'

fill_library() {
    python3 - "$work/library" <<'PY'
import shutil, sys
from pathlib import Path
library = Path(sys.argv[1])
shutil.rmtree(library, ignore_errors=True)
for folder, names in {"anna": ["vizimalmok.pdf", "szkennelt-csillagaszat.pdf", "halak-konyve.epub"],
                      "bela": ["titkos-receptek.docx"]}.items():
    (library / folder).mkdir(parents=True)
    for name in names:
        shutil.copy(Path("tests/e2e/fixtures") / name, library / folder / name)
PY
}

trial() {
    python3 scripts/e2e_trial.py \
        --base-url "https://localhost:$TEACHER_HTTPS_PORT" \
        --http-url "http://localhost:$TEACHER_PROXY_HTTP_PORT" \
        --library "$work/library" \
        --report "$work/report.md" \
        "$@"
}

if [ "$backend" = native ]; then
    native=scripts/e2e-native.sh
    export E2E_WORK="$work"
    "$native" down
    rm -rf "$work/data/metadata" "$work/data/qdrant" "$work/logs" "$work/run"
    fill_library
    TEACHER_AUTH_USERS=$("${E2E_PYTHON:?set E2E_PYTHON for --native}" -c "$hash_users_py")
    export TEACHER_AUTH_USERS TEACHER_AUTH_ADMINS=e2e-admin TEACHER_INGEST_TOKEN="$INGEST_TOKEN" OLLAMA_MODEL="$model"
    export STUDY_MAX_ATTEMPTS=${STUDY_MAX_ATTEMPTS:-4} OLLAMA_NUM_PREDICT=${OLLAMA_NUM_PREDICT:-512}
    export E2E_VOICE=$voice
    [ "$keep" = 1 ] || trap '"$native" down' EXIT
    "$native" up
    "$native" pull "$model"
    args=(--stack native --restart-cmd "$native restart" --point-count-cmd "$native point-count" --llm-model "$model")
    [ "$voice" = 0 ] || args+=(--voice)
    trial "${args[@]}"
    exit
fi

if ! docker compose version >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "FAIL: this trial needs a running Docker Engine with the Compose plugin." >&2
    echo "      Without Docker: scripts/e2e.sh --native, or the in-process flow in make test (tests/test_e2e_flow.py)." >&2
    exit 3
fi

env_file=$work/env
compose_files="-f compose/docker-compose.yml -f compose/docker-compose.dev.yml -f compose/docker-compose.proxy.yml"
[ "$voice" = 0 ] || compose_files="$compose_files -f compose/docker-compose.voice.example.yml"
compose() { docker compose --env-file "$env_file" $compose_files "$@"; }

write_env() { # users
    {
        cat .env.example
        cat <<ENV

# Trial settings, later lines win.
COMPOSE_PROJECT_NAME=teacher-e2e
TEACHER_HTTPS_PORT=$TEACHER_HTTPS_PORT
TEACHER_PROXY_HTTP_PORT=$TEACHER_PROXY_HTTP_PORT
TEACHER_DATA_DIR=$work/data
TEACHER_MOUNT_POINT=$work/library
TEACHER_TLS_DIR=$work/certs
TEACHER_ENABLE_VOICE=$([ "$voice" = 1 ] && echo true || echo false)
OLLAMA_MODEL=$model
INGEST_CONFIG_PATH=/app/config/ingestion/e2e.yml
STUDY_MAX_ATTEMPTS=${STUDY_MAX_ATTEMPTS:-4}
OLLAMA_NUM_PREDICT=${OLLAMA_NUM_PREDICT:-512}
VOICE_STT_MODEL=${VOICE_STT_MODEL:-large-v3-turbo}
TEACHER_AUTH_ADMINS=e2e-admin
TEACHER_INGEST_TOKEN=$INGEST_TOKEN
TEACHER_AUTH_USERS='$1'
ENV
    } >"$env_file"
}

mkdir -p "$work/data" "$work/certs"
fill_library
write_env ""
compose down --volumes --remove-orphans
echo "=== building the images (the first build downloads the embedding model) ==="
compose build
# Data from an earlier run belongs to root inside the containers, so a container removes it. The model stays.
compose run --rm --no-deps -v "$work/data:/wipe" --entrypoint sh api -c 'rm -rf /wipe/metadata /wipe/qdrant /wipe/logs'
users=$(compose run --rm --no-deps -T -e ANNA_PASSWORD -e BELA_PASSWORD -e CILI_PASSWORD -e ADMIN_PASSWORD \
    --entrypoint python api -c "$hash_users_py")
write_env "$users"

echo "=== starting the stack ==="
compose up -d --wait --wait-timeout 600
[ "$keep" = 1 ] || trap 'compose down --volumes --remove-orphans' EXIT

echo "=== pulling $model through the temporary edge connection ==="
TEACHER_ENV_FILE=$env_file bash scripts/model-update.sh "$model"

point_count='python -c "import os,json,urllib.request as u; r=u.Request(os.environ[\"QDRANT_URL\"]+\"/collections/\"+os.environ[\"QDRANT_COLLECTION\"]+\"/points/count\",data=b\"{\\\"exact\\\":true}\",headers={\"Content-Type\":\"application/json\"}); print(json.load(u.urlopen(r))[\"result\"][\"count\"])"'
compose_cmd="docker compose --env-file $env_file $compose_files"
args=(--stack compose
      --restart-cmd "$compose_cmd restart && $compose_cmd up -d --wait --wait-timeout 300"
      --point-count-cmd "$compose_cmd exec -T ingestion $point_count"
      --egress-cmd "TEACHER_ENV_FILE=$env_file bash scripts/test-egress-block.sh"
      --llm-model "$model")
[ "$voice" = 0 ] || args+=(--voice)
trial "${args[@]}"
