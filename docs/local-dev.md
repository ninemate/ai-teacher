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
3. Start the dev overlay:

```bash
make dev-up
```

4. Pull a model:

```bash
docker compose --env-file .env -f compose/docker-compose.yml -f compose/docker-compose.dev.yml exec ollama ollama pull qwen2.5:7b-instruct-q4_K_M
```

5. If you want LAN access during testing, set `TEACHER_HTTP_BIND=0.0.0.0` in `.env`.

6. Trigger indexing:

```bash
make test-ingest
curl -X POST http://127.0.0.1:8081/ingest/reindex \
  -H 'Content-Type: application/json' \
  -d '{"full_reindex": false, "remove_missing": true}'
```

7. Open `http://127.0.0.1:8080`.

## Dev Notes

- The default `.env.example` keeps the UI bound to `127.0.0.1`.
- Leave auth blank in dev only.
- For actual remote access, use VPN or enable auth plus reverse proxy.
