# Local Development

This repo can start in CPU-first dev mode without a GPU.

## Prerequisites

- Docker Engine
- Docker Compose plugin
- Enough RAM for Python services, Ollama, and Qdrant
- A mounted or local test library path

## Steps

1. Copy `.env.example` to `.env`.
2. Point `TEACHER_MOUNT_POINT` at a local folder or mounted share.
3. Set a login and the ingestion token in `.env`:

```bash
make hash-password                       # paste the output as TEACHER_AUTH_USERS='yourname:<hash>'
openssl rand -hex 32                     # paste as TEACHER_INGEST_TOKEN=
```

4. Start the dev overlay:

```bash
make dev-up
```

5. Pull a model:

```bash
docker compose --env-file .env -f compose/docker-compose.yml -f compose/docker-compose.dev.yml exec ollama ollama pull qwen2.5:7b-instruct-q4_K_M
```

6. If you want LAN access during testing, set `TEACHER_HTTP_BIND=0.0.0.0` in `.env`.

7. Trigger indexing:

```bash
make test-ingest
curl -X POST http://127.0.0.1:8081/ingest/reindex \
  -H "Authorization: Bearer $(sed -n 's/^TEACHER_INGEST_TOKEN=//p' .env)" \
  -H 'Content-Type: application/json' \
  -d '{"full_reindex": false, "remove_missing": true}'
```

8. Open `http://127.0.0.1:8080` and log in.

## Dev Notes

- The default `.env.example` keeps the UI bound to `127.0.0.1`.
- The login is on in dev too. `TEACHER_AUTH_DISABLED=true` turns it off for a purely local experiment; never on a reachable host.
- `make test` runs the unit tests (`pip install pytest` plus the api requirements first).
- `make test-chat` logs in and asks one question; it needs `TEACHER_USER` and `TEACHER_PASSWORD` in the environment.
- For actual remote access, use VPN plus HTTPS; the app login stays on.
