# Document Ingestion

The ingestion service scans the mounted share, extracts text, chunks it, creates embeddings, and updates Qdrant plus SQLite state.

## Supported Formats

Implemented in the MVP:

- PDF
- EPUB
- TXT
- Markdown
- DOCX

Known but not implemented in the MVP:

- MOBI
- AZW
- AZW3

For those formats, convert to EPUB or PDF first, or add Calibre-based conversion later.

## Watched Folders

The share holds terabytes, but only selected folders are indexed. They are listed under `watch.folders` in `config/ingestion/ingestion.yml` (copy it from `ingestion.example.yml`; override the location with `INGEST_CONFIG_PATH`):

- `path`: relative to the library mount point; `.` watches the whole library. Absolute paths and `..` are rejected.
- `enabled`: set `false` to keep an entry without scanning it.
- `recursive`: default `true`.
- `include` / `exclude`: glob patterns (case-insensitive; `*` also matches `/`) matched against the path inside the folder or the bare name. A matching directory in `exclude` is not entered at all. `watch.exclude` applies to every folder (for example `#recycle`, `@eaDir` on Synology).
- `extensions`: overrides `scan_extensions` for that folder.

Nothing outside the watched folders is walked. With no (active) folders configured a run indexes nothing and removes nothing.

Removal: with `remove_missing` (default on), documents not found in the watched folders any more are marked deleted and their vectors removed. That includes documents of a folder you stop watching. Documents under a watched folder that could not be read in this run (folder missing, share unmounted, unreadable directory) are left alone and the problem is reported as a run error. A watched folder that is completely empty (no entries at all) although it held indexed documents is treated the same way, because an unmounted share often leaves an empty mount point behind; to deliberately clear such a folder, remove it from `watch.folders`. A folder that still contains anything else (subfolders, other file types, filtered-out files) is a real change and its vanished documents are removed normally.

A full reindex (`full_reindex: true`) wipes the index before rebuilding it, so it is refused, leaving the index untouched, when no watched folders are configured, one of them is missing or unreadable (including nested directories found by the preview scan), or one looks unavailable by the rule above.

## Scheduled Indexing

Configured in the `schedule` section of the same file; disabled by default. Use either `interval_seconds` (at least 60) or a five-field `cron` expression with an optional `timezone` (default UTC). `run_on_startup` also starts a run when the service comes up. If the file has no `schedule` section, the legacy `INGEST_SCHEDULE_SECONDS` still enables a fixed interval. An invalid config stops the service at startup with an error.

The scheduler runs inside the ingestion service. Scheduled runs and `POST /ingest/reindex` share one lock, so runs never overlap: a manual request during a run gets `409`, a schedule slot that arrives during a run is skipped (missed slots are not queued).

`GET /ingest/schedule` returns the schedule (`enabled`, `interval_seconds`, `cron`, `next_run_at`, whether the last tick actually started a run), the watched folders, `running` / `current_file`, and `last_run`: `trigger` (`scheduled` or `manual`), `status`, `started_at`, `finished_at`, counts (`scanned`, `indexed`, `skipped`, `failed`, `deleted`), files scanned per folder, and up to 20 error messages. After a restart `last_run` is rebuilt from the database: status, counts, timestamps and the error messages, but not `trigger` or the per-folder counts. `GET /ingest/status` still returns the latest run as before.

## Incremental Behavior

Each file is tracked by:

- path
- file size
- modified timestamp
- SHA-256 hash when the file needs reprocessing

Files are compared by size and modified timestamp (one `stat` per file, with all known documents loaded in a single query) before anything else; only new or changed files are read and hashed. Unchanged files are skipped, which keeps a scheduled pass over a network share cheap. The first pass over a large folder still has to parse and embed everything.

## Metadata Stored

- source path
- title
- author when available
- language when detected
- chunk count
- last indexed time
- failure state and error message
- chunk locator such as page or section

## Limits and Tradeoffs

- Sequential indexing is the safe default for `16 GB RAM`.
- Chunk text is stored locally in the vector payload for retrieval.
- Scanned PDFs without embedded text are not handled yet because OCR is not included in the MVP.

