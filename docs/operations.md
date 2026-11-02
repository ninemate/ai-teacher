# Operations

## Routine Tasks

- deploy: `ansible-playbook -i ansible/inventory/dev.ini ansible/playbooks/deploy.yml`
- healthcheck: `make healthcheck` (container health + the egress audit)
- reindex: `make reindex` (incremental, `remove_missing: true`)
- last run: `make test-ingest` (ingestion `/ingest/status` with the token, asked through `docker compose exec` on the private network)
- model updates: `make model-update` (temporary edge-network connection for `ollama pull`; see docs/security.md)
- egress proof: `make test-egress` (config + containers + live probe)
- backup restore proof: `make test-backup-restore` (isolated SQLite plus a real Qdrant collection)
- component inventory: `make inventory` (local metadata only)
- logs: `make dev-logs`

## Watched Folders and Schedule

- List the folders to index under `watch.folders` in `config/ingestion/ingestion.yml` (copy `ingestion.example.yml`). Nothing else on the share is scanned, and with no folders a run indexes nothing.
- On a deployed host the file lives in `{{ teacher_stack_dir }}/config/ingestion/ingestion.yml`. Set the Ansible variable `teacher_ingest_config` (same structure as the YAML file) to have the deploy write it and restart `ingestion`. If you leave it empty, the deploy keeps the file already on the host and prints a warning when there is none. The local file is git-ignored.
- Scheduled indexing is the `schedule` section of the same file (interval or cron with a timezone). It is off by default. Manual `POST /ingest/reindex` keeps working and shares the same lock, so the two never overlap.
- `ingestion` reads the file at startup only, so restart the container after editing it by hand.
- Check `GET /ingest/schedule` for next run, last run, counts and errors. Ask it inside the private network (`make test-ingest` style, with the token), or through the API with a login (see `docs/document-ingestion.md`).
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

Run `make backup` on the host. It creates one UTC timestamped directory under `TEACHER_BACKUP_DIR` (default `TEACHER_DATA_DIR/backups`) and keeps the newest `BACKUP_KEEP_LAST` directories (default 7). Each directory contains a manifest with SHA-256 checksums, an SQLite backup made with SQLite's online backup API, a Qdrant collection snapshot made through Qdrant's API on the private Compose network, and the non-secret files under `config/ingestion`, `config/prompts` and `config/models`. It also includes the OCR cache and model-update records when present. No service route or host port is added.

The destination must be outside `LIBRARY_PATH`. The directory is mode `0700` and its files are mode `0600`, because the database contains password hashes and session records. Keep the backup storage itself protected and on a filesystem with enough room for the database, index snapshot and OCR cache.

Backups deliberately do not contain `.env`, Ansible Vault files, TLS private keys, or other secrets. Keep those separately in the approved secret store, along with any reverse-proxy config you use. The network library is not copied: it remains on the NAS and relies on its own snapshots. Documents, vectors and OCR cache can be rebuilt by reindexing from the library; login sessions, quizzes, decks and review history, Document Intelligence jobs and results, and other SQLite state require the database backup.

### Restore and undo

Stop the API and ingestion writers while leaving Qdrant running (`docker compose --env-file .env -f compose/docker-compose.yml stop api ingestion`), then run `make restore CONFIRM=YES`. The host tool launches a short lived worker on the private Compose network to use Qdrant; it does not need the API server running. To select a folder, use `make restore CONFIRM=YES RESTORE_FROM=backup-<timestamp>`. Restore checks every payload checksum before changing anything. If the live SQLite database is valid, restore saves the current state as a new backup, restores SQLite/config/Qdrant, and prints that undo backup's path. If the live database is missing or corrupt, it proceeds without a full undo point and reports that none is available. When an undo point exists and restore fails partway through, it attempts to restore that point automatically. To undo a successful restore, run the same command with the printed undo folder as `RESTORE_FROM`.

Restored `auth_sessions` are cleared, so all devices must sign in again. The undo backup is a normal timestamped backup and counts toward keep-last-N retention; copy it to protected storage if it must be kept longer. Start the stack again after restoring (`make dev-up`). Restoring config does not restore secrets from `.env` or Vault; review those separately before starting services.

### Manually approved model and component updates

1. **Review and approve:** inspect the model or component source and licence, evaluation results, version or digest, dependencies, and the previous `docs/component-inventory.md`. Record the exact approved artifacts and intended rollback point. Do not let an automatic job download or install models or images.
2. **Carry it offline:** on a connected staging machine, obtain only the approved Ollama artifacts and required container images. Verify their digests there, export container images with `docker save`, and carry the files to the offline host using approved transfer media. Keep an Ollama bundle's `models/manifests` and referenced `models/blobs` paths together.
3. **Install:** run `make backup` before changing installed state. Load approved image bundles with `docker load`. Before importing an Ollama bundle that replaces an existing tag, preserve that tag with `ollama cp <model:tag> <model:rollback-timestamp-digest>` inside the Ollama container. Merge bundle files into `TEACHER_DATA_DIR/ollama` without deleting prior manifests or blobs, then restart Ollama. The explicit `make model-update` pull path also makes a pre-update backup and preserves each old model under a digest-named rollback tag, but temporarily attaches Ollama to the existing edge network; use it only when that operator-approved path is allowed.
4. **Verify:** check with `ollama list`/`ollama show`, application smoke checks, `make test`, and `make inventory`. Compare the resulting digest and rollback path to the approval record. After using the temporary pull path, run `make test-egress` to verify the egress block is restored.
5. **Roll back:** keep the previous model tag and service image digests until acceptance. For a chat-model rollback, select its recorded `rollback-*` tag in `.env` and restart the stack. For service images, load and redeploy the recorded previous image digests. If application data or the index changed, restore the pre-update state backup too.

Embedding model changes need extra care. The current startup path creates a missing collection but does not check an existing collection's vector dimension or model identity. A different dimension makes Qdrant reject searches or upserts; a same-dimension model can leave old and new vectors mixed. The current full reindex clears/rebuilds watched workspaces but does not recreate the collection schema, so it is not a safe dimension migration. Keep the previous images and pre-change backup; if an embedding change has been applied, stop API/ingestion, restore the database and Qdrant backup, restore the old embedding model setting and old api/ingestion image digests, then start the old stack. Do not use an embedding model change as an in-place update until a collection migration is implemented.

`make inventory` refreshes the tracked component report using local metadata only. It records Ollama digests and install timestamps when Ollama is available, Python constraints/installed versions/licences from package metadata, configured voice and embedding models, local container image digests, and known rollback references. Missing license, date or checksum data is marked as unchecked/unavailable rather than inferred.

## Disk Management

Monitor:

- Ollama model cache
- Qdrant storage growth
- SQLite metadata growth
- the OCR cache (`metadata/ocr_cache`): entries of changed or deleted files are never removed
- logs

Local disk is finite and the index grows with the library, so size it for the model cache, vectors and OCR cache and keep the stack lean.
