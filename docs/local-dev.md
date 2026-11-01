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

The private services (api, ingestion, voice, qdrant, ollama) sit on an internal network with no route to the internet, and publish no host ports; only the HTTPS proxy is exposed (see [security.md](security.md)).

5. Pull a model — the private services have no egress, so model updates are an explicit step that briefly connects the Ollama container to the edge network:

```bash
make model-update   # pulls OLLAMA_MODEL from .env (add names as arguments for several)
```

The embedding model is baked into the api/ingestion images at build time; a new `EMBEDDING_MODEL` means a rebuild. This step also stocks the `make test-egress` probe image (it never pulls by itself).

6. If you want LAN or phone access during testing, set `TEACHER_PROXY_HOST` to the host's address and `TEACHER_PROXY_BIND=0.0.0.0` in `.env` (HTTPS proxy, see [https.md](https.md)). For anything beyond VPN testing, prefer the VPN: the stack has no other open port.

7. Trigger indexing (the ingestion container publishes no host port; this asks it through `docker compose exec`):

```bash
make test-ingest
```

8. Open `https://localhost:8443` and log in. The certificate comes from Caddy's local CA: either accept the browser warning or trust the root (`curl -o teacher-ca.crt http://localhost:8088/ca.crt`, see [https.md](https.md)). There is no direct plain-HTTP API port any more.

9. Prove the egress rule:

```bash
make test-egress
```

Checks the compose config, the running containers and a live probe from inside the private network (see [security.md](security.md)). Parts of it skip when the stack is not running.

## Dev Notes

- The default `.env.example` keeps the proxy bound to `127.0.0.1`.
- The login is on in dev too. `TEACHER_AUTH_DISABLED=true` turns it off for a purely local experiment; never on a reachable host.
- `make test` runs the unit tests (`pip install pytest` plus the api requirements first).
- `make test-chat` logs in through the proxy and asks one question; it needs `TEACHER_USER` and `TEACHER_PASSWORD` in the environment (`CURL_INSECURE=1` is set by default for the internal CA).
- `make dev-up` includes `compose/docker-compose.proxy.yml` (Caddy). Plain HTTP on port 8088 redirects to HTTPS.
- To poke a private service from the host, use `docker compose exec` (see `compose/docker-compose.dev.yml`).
- For actual remote access, use VPN plus HTTPS; the app login stays on.
