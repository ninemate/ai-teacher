# ai-teacher

Self-hosted Hungarian teacher-agent MVP for a private document library.

This repository is intentionally separate from any GPU image-generation stack. It has its own Compose files, Ansible playbooks, service names, data directories, documentation, and deployment assumptions.

## What This Repo Implements

- Mounted network-share based document library access
- Incremental document ingestion for PDF, EPUB, TXT, Markdown, DOCX, and DRM-free MOBI/AZW/AZW3, limited to configured watched folders
- Scheduled indexing (interval or cron) inside the ingestion service, plus manual reindex
- OCR (Tesseract, `hun+eng`) for scanned PDF pages, cached locally and resumed across runs
- Local metadata state in SQLite
- Local vector search in Qdrant
- Local LLM runtime via Ollama
- Hungarian-first RAG chat API and mobile-friendly web UI behind a built-in login (argon2 users, sessions, CSRF)
- Stored quizzes and flashcard decks generated from the library, with source references, spaced-repetition review for cards and quiz attempt history (`/study/...`)
- Ansible-driven deployment to a separate VM
- Optional NVIDIA and optional voice scaffolding

## Chosen MVP Stack

- UI and API: custom `FastAPI` service
- RAG storage: `Qdrant`
- Metadata and ingestion state: `SQLite`
- Local inference runtime: `Ollama`
- Embeddings: `sentence-transformers` with `intfloat/multilingual-e5-base`
- Ingestion glue: custom lightweight service reading from the mounted share
- Remote access: prefer `WireGuard` or `Tailscale`; optional reverse proxy

Why this stack:

- It stays small enough for `1x RTX 3070`, `8 GB VRAM`, `16 GB RAM`
- It avoids a heavy all-in-one AI platform
- It keeps custom code limited to ingestion and RAG glue
- It is easy to migrate to another VM later

## Resource Expectations

- Do not assume the full library fits on local SSD.
- Source documents remain on the mounted share.
- Indexed chunk text, vectors, SQLite metadata, model cache, and logs are stored locally.
- 7B to 8B quantized chat models are the practical starting point for `8 GB VRAM`.
- Voice is optional because Hungarian STT/TTS quality and resource use need separate validation.

## Quick Start

1. Copy `.env.example` to `.env` and adjust paths and credentials. Set `TEACHER_AUTH_USERS` (`make hash-password`) and `TEACHER_INGEST_TOKEN` (`openssl rand -hex 32`).
2. Ensure your network share is mounted at `TEACHER_MOUNT_POINT`.
3. Copy `config/ingestion/ingestion.example.yml` to `config/ingestion/ingestion.yml` and list the folders to index under `watch.folders` (paths relative to the share; `.` is the whole share). Without it nothing is indexed. The same file turns on scheduled indexing.
4. Start the local dev stack:

```bash
make dev-up
```

5. Pull a chat model into Ollama from inside the running container:

```bash
docker compose --env-file .env -f compose/docker-compose.yml -f compose/docker-compose.dev.yml exec ollama ollama pull qwen2.5:7b-instruct-q4_K_M
```

6. If you need LAN or phone access during testing, set `TEACHER_PROXY_HOST` and `TEACHER_PROXY_BIND=0.0.0.0` in `.env`; the HTTPS proxy and certificate trust are described in [docs/https.md](docs/https.md).

7. Trigger indexing (or wait for the schedule):

```bash
curl -X POST http://127.0.0.1:8081/ingest/reindex \
  -H "Authorization: Bearer $(sed -n 's/^TEACHER_INGEST_TOKEN=//p' .env)" \
  -H 'Content-Type: application/json' \
  -d '{"full_reindex": false, "remove_missing": true}'
```

8. Open the UI and log in:

```text
https://localhost:8443
```

The certificate is from Caddy's local CA; see [docs/https.md](docs/https.md) to trust it. The API is also on `http://127.0.0.1:8080` on this machine.

## Tests

```bash
pip install pytest sqlalchemy pydantic-settings fastapi httpx \
  argon2-cffi python-multipart jinja2 pyyaml croniter tzdata \
  PyMuPDF Pillow pytesseract beautifulsoup4 ebooklib mobi lingua-language-detector python-docx
python -m pytest
```

CI (`.github/workflows/ci.yml`) installs both services' requirements and runs `ruff check services/`,
`mypy --ignore-missing-imports services/` and `pytest`, then builds the images and deploys `main` to the self-hosted VM.

The study tests use a mocked LLM and retriever; no Ollama or Qdrant is needed. The OCR tests mock Tesseract;
a smoke test with the real engine runs only when `tesseract` with the `hun` and `eng` language packs is installed.
The OCR-through-ingestion tests also need `qdrant-client` (or the full `services/ingestion/requirements.txt`)
and are skipped without it; the embedding model is stubbed.

## Architecture

```mermaid
flowchart TD
  Phone[Father's Phone] -->|VPN or HTTPS| UI[FastAPI Web UI]
  UI --> API[Teacher API]
  API --> OLLAMA[Ollama Runtime]
  API --> QDRANT[(Qdrant)]
  API --> SQLITE[(SQLite Metadata)]
  API -->|reindex, schedule status| INGEST
  Share[Mounted Network Share] -->|watched folders| INGEST[Ingestion Service + scheduler + OCR]
  INGEST --> QDRANT
  INGEST --> SQLITE
```

## Security Warnings

- Do not expose the UI publicly. Use a VPN, with HTTPS in front; the app login is a further layer, not a replacement.
- Do not commit network-share credentials or `.env`.
- Prefer `WireGuard` or `Tailscale` for phone access.
- The UI and API require a login (`TEACHER_AUTH_USERS`, argon2 hashes); see `docs/security.md`.
- The ingestion `/ingest` endpoints require `TEACHER_INGEST_TOKEN`.
- Indexed chunk text is stored locally. Treat the local SSD as sensitive data.

## Repo Layout

The repo is structured for deployment first:

- `compose/` for runtime definitions
- `ansible/` for deployment and share mounting
- `services/` for the thin custom glue services
- `config/` for prompts and operator-facing examples
- `docs/` for architecture, ops, security, and staged voice decisions

## Current Status

Implemented now:

- Separate deployable stack (Compose, Ansible, CI deploy to a self-hosted VM)
- Text-first RAG chat with sources, Hungarian prompts
- Ingestion of watched folders only, incremental (size + mtime, then hash), with safe handling of an unmounted share
- Scheduled indexing (interval or cron, `run_on_startup`), sharing one lock with manual reindex; status at `GET /ingest/schedule`
- OCR for scanned PDF pages: per-page cache, per-file time and page limits, time-limited files resumed by later runs, interrupted runs recorded and picked up again
- App login for the UI and every API route (argon2 users from config, server-side sessions, CSRF, login rate limits); ingestion endpoints behind a bearer token
- Stored quizzes and flashcard decks with source references, SM-2 card review and quiz attempt history
- Mobile-friendly web UI
- HTTPS in front of the UI via an optional Caddy overlay (`compose/docker-compose.proxy.yml`, see [docs/https.md](docs/https.md))
- MOBI/AZW/AZW3 ingestion (pure-Python unpack to EPUB/HTML; DRM-protected books are skipped with a logged reason)
- Voice service: Whisper STT scaffold behind an optional overlay; TTS answers 501

Not implemented yet:

- Real Hungarian offline STT/TTS backend wiring
- OCR for image-only EPUB/DOCX
- SSO or an identity provider (users are configured in `.env`)

## Next Docs

- [Architecture](docs/architecture.md)
- [Ansible Deploy](docs/ansible-deploy.md)
- [Network Share](docs/network-share.md)
- [Document Ingestion](docs/document-ingestion.md)
- [Model Selection](docs/model-selection.md)
- [Mobile Access](docs/mobile-access.md)
- [Voice Hungarian](docs/voice-hungarian.md)
- [Security](docs/security.md)
- [HTTPS Reverse Proxy](docs/https.md)
- [Operations](docs/operations.md)
- [Open Questions](docs/open-questions.md)
