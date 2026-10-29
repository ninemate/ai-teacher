# Operations

## Routine Tasks

- deploy: `ansible-playbook -i ansible/inventory/dev.ini ansible/playbooks/deploy.yml`
- healthcheck: `make healthcheck`
- reindex: `make reindex`
- logs: `make dev-logs`

## Reindex Strategy

- manual reindex is the safest starting point
- list the folders to index under `watch.folders` in `config/ingestion/ingestion.yml`; nothing else on the share is scanned
- scheduled indexing is enabled in the `schedule` section of that file (interval or cron); it is off by default and manual `POST /ingest/reindex` keeps working, sharing the same lock
- check `GET /ingest/schedule` for last run, next run, counts and errors (see `docs/document-ingestion.md`)
- if a watched folder is missing (share not mounted), its documents are kept and the run reports an error
- full reindex should be reserved for parser changes or index corruption recovery

## Backups

Back up at least:

- `teacher_data_dir/metadata`
- `teacher_data_dir/qdrant`
- `.env` or Vault-backed secrets source
- reverse-proxy config if used

The network share itself is not duplicated here.

## Disk Management

Monitor:

- Ollama model cache
- Qdrant storage growth
- SQLite metadata growth
- logs

The local SSD is only `240 GB`, so keep the stack lean.

