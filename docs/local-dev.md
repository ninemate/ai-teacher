# Local Development

This repo can start in CPU-first dev mode without a GPU.

## Prerequisites

- Docker Engine
- Docker Compose plugin
- Enough RAM for Python services, Ollama, and Qdrant
- A mounted or local test library path

## Steps

1. Copy `.env.example` to `.env`.
2. Point `TEACHER_MOUNT_POINT` at a local folder or mounted share, and `TEACHER_DATA_DIR` at a folder for the stack's data. To use a CIFS share without Ansible, mount it by hand or via `/etc/fstab` as described in [network-share.md](network-share.md#without-ansible), then check it with `make check-library` (`REQUIRE_MOUNTPOINT=0` for a plain local folder). Where bind propagation is not supported (Docker Desktop), set `TEACHER_MOUNT_PROPAGATION=rprivate` and mount the share before starting the stack. Without a GPU, set `OLLAMA_MODEL` to a small model such as `qwen2.5:0.5b`; the default is slow on a CPU.
3. Set a login and the ingestion token in `.env`:

```bash
make hash-password                       # paste the output as TEACHER_AUTH_USERS='yourname:<hash>'
openssl rand -hex 32                     # paste as TEACHER_INGEST_TOKEN=
```

Keep the single quotes: the hash contains `$`. The first `make hash-password` builds the api image, which takes a while because it downloads the embedding model. Add your name to `TEACHER_AUTH_ADMINS` if you want to start indexing from the API and see its status.

4. Choose what to index: copy `config/ingestion/ingestion.example.yml` to `config/ingestion/ingestion.yml` and list your folders under `watch.folders`, with paths relative to `TEACHER_MOUNT_POINT` and `workspace: user:yourname`. Without this file nothing is indexed.

5. Start the dev overlay:

```bash
make dev-up
```

The private services (api, ingestion, voice, qdrant, ollama) sit on an internal network with no route to the internet, and publish no host ports; only the HTTPS proxy is exposed (see [security.md](security.md)).

6. Pull a model — the private services have no egress, so model updates are an explicit step that briefly connects the Ollama container to the edge network:

```bash
make model-update   # pulls OLLAMA_MODEL from .env (add names as arguments for several)
```

The embedding model is baked into the api/ingestion images at build time; a new `EMBEDDING_MODEL` means a rebuild. This step also stocks the `make test-egress` probe image (it never pulls by itself).

7. If you want LAN or phone access during testing, set `TEACHER_PROXY_HOST` to the host's address and `TEACHER_PROXY_BIND=0.0.0.0` in `.env` (HTTPS proxy, see [https.md](https.md)). For anything beyond VPN testing, prefer the VPN: the stack has no other open port.

8. Trigger indexing and follow it (the ingestion container publishes no host port; these ask it through `docker compose exec`):

```bash
make ingest-now    # starts a run of the watched folders
make test-ingest   # status and counts of the last run
```

9. Open `https://localhost:8443` and log in. The certificate comes from Caddy's local CA: either accept the browser warning or trust the root (`curl -o teacher-ca.crt http://localhost:8088/ca.crt`, see [https.md](https.md)). There is no direct plain-HTTP API port any more.

10. Prove the egress rule:

```bash
make test-egress
```

Checks the compose config, the running containers and a live probe from inside the private network (see [security.md](security.md)). Parts of it skip when the stack is not running.

## Dev Notes

- The default `.env.example` keeps the proxy bound to `127.0.0.1`.
- The login is on in dev too. `TEACHER_AUTH_DISABLED=true` turns it off for a purely local experiment; never on a reachable host.
- `make test` runs the unit tests (`pip install pytest httpx`, plus `services/api/requirements.txt` and `services/ingestion/requirements.txt` first). It includes the end-to-end flow in one process, with a fake model and embedder (`tests/test_e2e_flow.py`).
- `make test-e2e` tries the whole stack: its own copy of it (project `teacher-e2e`, ports 18443 and 18088, data in `.scratch/e2e`) with a small model and Hungarian fixtures. See [Testing the whole stack](#testing-the-whole-stack).
- `make test-chat` logs in through the proxy and asks one question; it needs `TEACHER_USER` and `TEACHER_PASSWORD` in the environment (`CURL_INSECURE=1` is set by default for the internal CA).
- `make dev-up` includes `compose/docker-compose.proxy.yml` (Caddy). Plain HTTP on port 8088 redirects to HTTPS.
- To poke a private service from the host, use `docker compose exec` (see `compose/docker-compose.dev.yml`).
- For actual remote access, use VPN plus HTTPS; the app login stays on.

## Testing the whole stack

```bash
make test-e2e                 # needs Docker; about the time of a first build plus a few minutes
scripts/e2e.sh --voice        # also a Hungarian speech round trip (the voice image downloads large models)
scripts/e2e.sh --keep         # leaves the stack up to look around
```

It builds and starts everything, pulls `E2E_OLLAMA_MODEL` (default `qwen2.5:0.5b`), and goes through the HTTPS proxy as a browser would: login, indexing of a text PDF, a scanned PDF, an EPUB and a DOCX, citations, a question with no source, isolation between two accounts, stored quizzes and flashcards across a restart, deleting a document, and `make test-egress`. It prints a table of what passed, failed or was skipped, and leaves it in `.scratch/e2e/report.md`. A dev stack on the same host is not touched. The assertions check structure, citations and storage, not the model's wording.

Where Docker cannot run, `scripts/e2e.sh --native` runs the same steps against the services as plain processes (see `scripts/e2e-native.sh` for the binaries it needs). That proves the application, but not the images, the compose wiring or the egress rule.
