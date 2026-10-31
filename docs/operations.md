# Operations

## Routine Tasks

- deploy: `ansible-playbook -i ansible/inventory/dev.ini ansible/playbooks/deploy.yml`
- healthcheck: `make healthcheck`
- reindex: `make reindex` (incremental, `remove_missing: true`)
- last run: `make test-ingest` (ingestion `/ingest/status` with the token from `.env`)
- logs: `make dev-logs`

## Watched Folders and Schedule

- List the folders to index under `watch.folders` in `config/ingestion/ingestion.yml` (copy `ingestion.example.yml`). Nothing else on the share is scanned, and with no folders a run indexes nothing.
- On a deployed host the file lives in `{{ teacher_stack_dir }}/config/ingestion/ingestion.yml`. Set the Ansible variable `teacher_ingest_config` (same structure as the YAML file) to have the deploy write it and restart `ingestion`. If you leave it empty, the deploy keeps the file already on the host and prints a warning when there is none. The local file is git-ignored.
- Scheduled indexing is the `schedule` section of the same file (interval or cron with a timezone). It is off by default. Manual `POST /ingest/reindex` keeps working and shares the same lock, so the two never overlap.
- `ingestion` reads the file at startup only, so restart the container after editing it by hand.
- Check `GET /ingest/schedule` for next run, last run, counts and errors. Use it on the ingestion port with the token, or through the API with a login (see `docs/document-ingestion.md`).
- If a watched folder is missing (share not mounted), its documents are kept and the run reports an error.
- Reserve a full reindex for parser changes, OCR settings changes that are not picked up automatically (page limit, Tesseract installed later), or index corruption recovery. It is refused while a watched folder looks unavailable.

## Long OCR Runs

- OCR is CPU-only and runs inside the indexing run, one file at a time. The first pass over a scanned library can take hours. Scheduled ticks during it are skipped, and manual reindex answers `409`.
- `OCR_TIMEOUT_SECONDS` (default 900) bounds one file per run. A file cut off by it is continued by the next runs until it is complete, so a nightly schedule works through a large scan over several nights. Each OCR'd page is cached at once.
- Look in the `ocr_documents` table (SQLite) for files still `partial` or marked "Stalled"; the message says why.

## Restarts and Deploys

- A deploy (`docker compose up -d --build`), `restart.yml` or a host reboot stops a running index. Nothing is corrupted. At startup the run is recorded as `interrupted`, finished files stay indexed, and finished OCR pages stay cached. The next run (schedule, `run_on_startup`, or `make reindex`) continues with the remaining files.
- An interrupted full reindex leaves a partly rebuilt index; the next incremental run fills in the rest.
- Login sessions are in SQLite and survive restarts; the login rate-limit counters do not.

## Backups

Back up at least:

- `teacher_data_dir/metadata` (SQLite: documents, runs, login sessions, quizzes, decks and review history; plus `ocr_cache/`, which is expensive to rebuild)
- `teacher_data_dir/qdrant`
- `.env` or Vault-backed secrets source (`TEACHER_AUTH_USERS`, `TEACHER_INGEST_TOKEN`)
- `config/ingestion/ingestion.yml`, unless it comes from `teacher_ingest_config`
- reverse-proxy config if used

Stop the stack (or at least `ingestion` and `api`) before copying the SQLite file, or use `sqlite3 teacher.db ".backup copy.db"`.

The network share itself is not duplicated here.

## Disk Management

Monitor:

- Ollama model cache
- Qdrant storage growth
- SQLite metadata growth
- the OCR cache (`metadata/ocr_cache`): entries of changed or deleted files are never removed
- logs

Local disk is finite and the index grows with the library, so size it for the model cache, vectors and OCR cache and keep the stack lean.
