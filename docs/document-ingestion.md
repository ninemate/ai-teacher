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

The scheduler runs inside the ingestion service. Scheduled runs and `POST /ingest/reindex` share one lock, so runs never overlap: a manual request during a run gets `409` (also through the API's `/ingest/reindex`), a schedule slot that arrives during a run is skipped (missed slots are not queued). The next slot is counted from the end of the run, so a long OCR run simply delays it.

The service reads `ingestion.yml` at startup only; restart the `ingestion` container after editing it (the Ansible deploy does this when it writes the file).

`GET /ingest/schedule` (bearer token like the other `/ingest` endpoints; logged-in users get the same through the API at `GET /ingest/schedule`) returns the schedule (`enabled`, `interval_seconds`, `cron`, `next_run_at`, whether the last tick actually started a run), the watched folders, `running` / `current_file`, and `last_run`: `trigger` (`scheduled` or `manual`), `status`, `started_at`, `finished_at`, counts (`scanned`, `indexed`, `skipped`, `failed`, `deleted`), files scanned per folder, and up to 20 error messages. After a restart `last_run` is rebuilt from the database: status, counts, timestamps and the error messages, but not `trigger` or the per-folder counts. `GET /ingest/status` still returns the latest run as before.

A run whose process stopped (deploy, restart, crash) is marked `interrupted` when the service starts again. What it finished stays indexed, and the next run picks up the rest.

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
- OCR is CPU-only (Tesseract) and slow: roughly several seconds per page at 300 DPI. Large scanned books can
  take minutes to hours, so use the per-file limits below and expect the first pass over a scanned library to be long.
- Only PDFs are OCR'd. Image-only EPUB/DOCX files are not handled.

## OCR for scanned PDFs

Each PDF page is checked for a text layer. A page with fewer than `OCR_MIN_CHARS` characters (default `50`)
is treated as scanned, rendered at `OCR_DPI` and read with Tesseract using `OCR_LANGUAGE`
(`hun+eng` by default; the image ships `tesseract-ocr-hun` and `tesseract-ocr-eng`). Pages that already
have text are used as-is, so text PDFs behave exactly as before and mixed PDFs are only OCR'd where needed.
OCR'd chunks get the locator `oldal N (OCR)`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCR_ENABLED` | `true` | Turn OCR on or off |
| `OCR_LANGUAGE` | `hun+eng` | Tesseract languages |
| `OCR_DPI` | `300` | Render resolution |
| `OCR_MIN_CHARS` | `50` | Text-layer length below which a page counts as scanned |
| `OCR_TIMEOUT_SECONDS` | `900` | Time budget per file (`0` = unlimited) |
| `OCR_MAX_PAGES` | `0` | Max OCR'd pages per file (`0` = unlimited) |
| `OCR_CACHE_DIR` | `/data/metadata/ocr_cache` | Local cache location |

Behaviour:

- **Read-only source.** The share is never written to. OCR text is cached as JSON under `OCR_CACHE_DIR`
  (inside the existing metadata volume), keyed by file hash, language and DPI. The cache is written after
  every OCR'd page, so a stopped run loses at most the page in progress.
- **Not redone.** Incremental runs skip unchanged files as before; a full reindex or a changed-but-identical
  file reuses the cached page text instead of running Tesseract again. Changing language or DPI re-OCRs.
- **SQLite record.** The `ocr_documents` table records, per document, the OCR language, DPI, pages OCR'd,
  pages left pending and a status: `complete`, `partial` (time/page limit hit or a page failed) or
  `unavailable` (Tesseract missing) or `disabled` (indexed with OCR off while it had scanned pages;
  re-enabling OCR re-processes it on the next run). PDFs indexed before OCR tracking existed need one full reindex.
- **Limits.** When the time or page limit is hit, the pages finished so far are indexed and cached and the
  rest are left pending (`partial`).
  - Time limit: the next run (scheduled or manual, incremental) processes the file again. Cached pages
    cost nothing, so it spends a fresh `OCR_TIMEOUT_SECONDS` on the next pages. A large scan is finished
    over several runs, and each run re-embeds the pages indexed so far. If a run adds no page (a single page
    takes longer than the whole budget), the record is marked "Stalled" and the file is no longer retried.
    Raise the limit and run a full reindex.
  - Page limit (`OCR_MAX_PAGES`, which counts cached pages too): not resumed. Raise the limit and run a full
    reindex; cached pages are not repeated.
- **Failures stay local.** A broken page or file never aborts the run. A scanned PDF that failed outright is
  retried on the next run (cheap thanks to the cache) while OCR is enabled, unless its OCR record shows a stop
  that a retry would repeat (stalled, page limit, failing page). A page that failed (status `partial`
  with "OCR failed on page N") is not retried automatically; a full reindex tries it again.
- **Tesseract missing** (`unavailable`): the text-layer pages are indexed. After installing it, run a full reindex.
- **Cache growth.** Cache files of changed or deleted documents are not removed. Delete `OCR_CACHE_DIR` to
  reclaim the space; the next full reindex rebuilds what is still needed.

