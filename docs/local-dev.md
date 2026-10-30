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

6. If you want LAN or phone access during testing, set `TEACHER_PROXY_HOST` to the host's address and `TEACHER_PROXY_BIND=0.0.0.0` in `.env` (HTTPS proxy, see [https.md](https.md)). Leave `TEACHER_HTTP_BIND` on `127.0.0.1`.

7. Trigger indexing:

```bash
make test-ingest
curl -X POST http://127.0.0.1:8081/ingest/reindex \
  -H "Authorization: Bearer $(sed -n 's/^TEACHER_INGEST_TOKEN=//p' .env)" \
  -H 'Content-Type: application/json' \
  -d '{"full_reindex": false, "remove_missing": true}'
```

8. Open `https://localhost:8443` and log in. The certificate comes from Caddy's local CA: either accept the browser warning or trust the root (`curl -o teacher-ca.crt http://localhost:8088/ca.crt`, see [https.md](https.md)). `http://127.0.0.1:8080` still reaches the API directly on this machine, without TLS.

## Dev Notes

- The default `.env.example` keeps the UI bound to `127.0.0.1`.
- The login is on in dev too. `TEACHER_AUTH_DISABLED=true` turns it off for a purely local experiment; never on a reachable host.
- `make test` runs the unit tests (`pip install pytest` plus the api requirements first).
- `make test-chat` logs in and asks one question; it needs `TEACHER_USER` and `TEACHER_PASSWORD` in the environment.
- `make dev-up` includes `compose/docker-compose.proxy.yml` (Caddy). Plain HTTP on port 8088 redirects to HTTPS.
- For actual remote access, use VPN plus HTTPS; the app login stays on.
