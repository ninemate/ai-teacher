#!/bin/bash
# The stack of scripts/e2e.sh without containers, for hosts where Docker cannot run: the same services, images'
# versions and Caddyfile as plain processes on this machine, on loopback. It proves the application end to end, but
# not the images, compose wiring or the network rule; only scripts/e2e.sh does that.
#
# Usage: scripts/e2e-native.sh up|restart|down|point-count   (reads the settings from the environment, see below)
#   E2E_WORK        working folder (library, data, logs)
#   E2E_BIN         folder with the binaries: qdrant, caddy, ollama
#   E2E_PYTHON      python with services/api and services/ingestion requirements
#   E2E_TESSERACT   optional tesseract install prefix when it is not on PATH
#   E2E_VOICE=1     also start the voice service, with E2E_VOICE_PYTHON (services/voice requirements) and
#                   VOICE_TTS_VOICE_DIR (a folder of Piper voices)
set -eu

repo=$(CDPATH="" cd -- "$(dirname -- "$0")/.." && pwd)
work=${E2E_WORK:?set E2E_WORK}
bin=${E2E_BIN:?set E2E_BIN}
python=${E2E_PYTHON:?set E2E_PYTHON}
https_port=${TEACHER_HTTPS_PORT:-18443}
http_port=${TEACHER_PROXY_HTTP_PORT:-18088}
qdrant_port=${E2E_QDRANT_PORT:-16333}
ollama_port=${E2E_OLLAMA_PORT:-11435}
api_port=${E2E_API_PORT:-18000}
ingestion_port=${E2E_INGESTION_PORT:-18081}
voice_port=${E2E_VOICE_PORT:-18090}

run=$work/run
data=$work/data
mkdir -p "$run" "$data/metadata" "$data/qdrant" "$data/caddy" "$work/logs"

export METADATA_DB_URL="sqlite:///$data/metadata/teacher.db"
export LIBRARY_PATH="$work/library"
export QDRANT_URL="http://127.0.0.1:$qdrant_port"
export QDRANT_COLLECTION=teacher_documents
export OLLAMA_BASE_URL="http://127.0.0.1:$ollama_port"
export EMBEDDING_DEVICE=cpu
export EMBEDDING_MODEL=${EMBEDDING_MODEL:-intfloat/multilingual-e5-base}
export TEACHER_PROMPT_PATH="$repo/config/prompts/teacher_hu.md"
export INGEST_CONFIG_PATH="$repo/config/ingestion/e2e.yml"
export INGESTION_BASE_URL="http://127.0.0.1:$ingestion_port"
export VOICE_BASE_URL="http://127.0.0.1:$voice_port"
export OCR_CACHE_DIR="$data/metadata/ocr_cache"
export PYTHONPATH="$repo/services/common"
if [ -n "${E2E_TESSERACT:-}" ]; then
    export PATH="$E2E_TESSERACT/usr/bin:$PATH"
    export LD_LIBRARY_PATH="$E2E_TESSERACT/usr/lib/x86_64-linux-gnu"
    export TESSDATA_PREFIX="$E2E_TESSERACT/usr/share/tesseract-ocr/5/tessdata"
fi

start() { # name, working dir, command...
    name=$1; dir=$2; shift 2
    (cd "$dir" && exec nohup "$@" >>"$work/logs/$name.log" 2>&1) &
    echo $! >"$run/$name.pid"
}

wait_http() { # url, seconds
    deadline=$(($(date +%s) + $2))
    until curl -fsS -k -o /dev/null --max-time 5 "$1" 2>/dev/null; do
        [ "$(date +%s)" -lt "$deadline" ] || { echo "FAIL: $1 did not answer within $2s" >&2; return 1; }
        sleep 1
    done
}

start_ollama() {
    OLLAMA_HOST="127.0.0.1:$ollama_port" OLLAMA_MODELS="$data/ollama" OLLAMA_NO_UPDATE_CHECK=1 \
        start ollama "$work" "$bin/ollama" serve
    wait_http "$OLLAMA_BASE_URL/api/tags" 60
}

start_stack() {
    QDRANT__SERVICE__HTTP_PORT=$qdrant_port QDRANT__SERVICE__GRPC_PORT=$((qdrant_port + 1)) \
        QDRANT__SERVICE__HOST=127.0.0.1 QDRANT__STORAGE__STORAGE_PATH="$data/qdrant" QDRANT__TELEMETRY_DISABLED=true \
        start qdrant "$work" "$bin/qdrant"
    wait_http "$QDRANT_URL/healthz" 60
    start_ollama
    APP_ROLE=api start api "$repo/services/api/app" "$python" -m uvicorn main:app --host 127.0.0.1 --port "$api_port"
    APP_ROLE=ingestion start ingestion "$repo/services/ingestion/app" \
        "$python" -m uvicorn main:app --host 127.0.0.1 --port "$ingestion_port"
    wait_http "http://127.0.0.1:$api_port/health" 300
    wait_http "http://127.0.0.1:$ingestion_port/health" 300
    if [ "${E2E_VOICE:-0}" = 1 ]; then
        start voice "$repo/services/voice/app" "${E2E_VOICE_PYTHON:?set E2E_VOICE_PYTHON}" \
            -m uvicorn main:app --host 127.0.0.1 --port "$voice_port"
        wait_http "$VOICE_BASE_URL/health" 120
    fi

    # The committed Caddyfile; only what compose gives it (service name, data folder, published ports) is replaced.
    sed -e "s#reverse_proxy api:8000#reverse_proxy 127.0.0.1:$api_port#" \
        -e "s#/data/caddy/#$data/caddy/caddy/#" \
        -e "s#auto_https disable_redirects#auto_https disable_redirects\n\tdefault_bind 127.0.0.1\n\thttp_port $http_port\n\thttps_port $https_port\n\tskip_install_trust#" \
        "$repo/compose/proxy/Caddyfile" >"$run/Caddyfile"
    XDG_DATA_HOME=$data/caddy XDG_CONFIG_HOME=$data/caddy TEACHER_PROXY_HOST=localhost TEACHER_HTTPS_PORT=$https_port \
        TEACHER_TLS=internal TEACHER_HSTS=max-age=0 \
        start caddy "$work" "$bin/caddy" run --config "$run/Caddyfile" --adapter caddyfile
    wait_http "https://localhost:$https_port/health" 60
}

stop_stack() {
    for name in caddy voice ingestion api ollama qdrant; do
        [ -f "$run/$name.pid" ] || continue
        pid=$(cat "$run/$name.pid")
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$run/$name.pid"
    done
}

case "${1:-}" in
    up) start_stack ;;
    down) stop_stack ;;
    restart) stop_stack; start_stack ;;
    pull) "$bin/ollama" --version >/dev/null; OLLAMA_HOST="127.0.0.1:$ollama_port" "$bin/ollama" pull "$2" ;;
    point-count)
        curl -fsS -X POST -H 'Content-Type: application/json' -d '{"exact":true}' \
            "$QDRANT_URL/collections/$QDRANT_COLLECTION/points/count" \
            | "$python" -c 'import json,sys; print(json.load(sys.stdin)["result"]["count"])'
        ;;
    *) echo "usage: $0 up|down|restart|pull <model>|point-count" >&2; exit 2 ;;
esac
