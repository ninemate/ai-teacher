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

## Incremental Behavior

Each file is tracked by:

- path
- file size
- modified timestamp
- SHA-256 hash when the file needs reprocessing

Unchanged files are skipped.

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
  (inside the existing metadata volume), keyed by file hash, language and DPI.
- **Not redone.** Incremental runs skip unchanged files as before; a full reindex or a changed-but-identical
  file reuses the cached page text instead of running Tesseract again. Changing language or DPI re-OCRs.
- **SQLite record.** The `ocr_documents` table records, per document, the OCR language, DPI, pages OCR'd,
  pages left pending and a status: `complete`, `partial` (time/page limit hit or a page failed) or
  `unavailable` (Tesseract missing) or `disabled` (indexed with OCR off while it had scanned pages;
  re-enabling OCR re-processes it on the next run). PDFs indexed before OCR tracking existed need one full reindex.
- **Limits.** When the time or page limit is hit, the pages finished so far are indexed and cached and the
  rest are left pending (`partial`). Raise the limit and run a full reindex to finish; cached pages are not repeated.
- **Failures stay local.** A broken page or file never aborts the run. A scanned PDF that failed outright is
  retried on the next run (cheap thanks to the cache) while OCR is enabled.

