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
- Scanned PDFs without embedded text are not handled yet because OCR is not included in the MVP.

