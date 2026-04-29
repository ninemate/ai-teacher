# Operations

## Routine Tasks

- deploy: `ansible-playbook -i ansible/inventory/dev.ini ansible/playbooks/deploy.yml`
- healthcheck: `make healthcheck`
- reindex: `make reindex`
- logs: `make dev-logs`

## Reindex Strategy

- manual reindex is the safest starting point
- scheduled indexing can be enabled with `INGEST_SCHEDULE_SECONDS`
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

