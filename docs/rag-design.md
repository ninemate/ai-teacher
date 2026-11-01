# RAG Design

This project uses a straightforward retrieval-augmented generation flow rather than an agentic orchestration platform.

## Retrieval Steps

1. Embed the user question with a multilingual embedding model.
2. Search Qdrant for top matching chunks.
3. Assemble a bounded context window.
4. Add the Hungarian teacher prompt.
5. Ask the local model to answer only from retrieved context.

## Teacher Behavior

The default prompt instructs the model to:

- answer in Hungarian
- explain in a teacher-like style
- adapt to requested level
- admit when the library does not contain enough information
- cite the numbered sources (`[1]`) and mark its own statements (`[forrás nélkül]`), see "Source Citations" below
- suggest quiz or flashcard style follow-ups

## Source Citations

Every answer from `/chat`, `/summary`, `/quiz` and `/flashcards` says which file, page or chapter it came from, which statements are the model's own, and when a source is uncertain.

### What a chunk carries

Ingestion keeps these fields on each chunk and in its Qdrant payload, next to the existing `source_path`, `title` and `locator`:

| field | meaning |
| --- | --- |
| `file_id` | SHA-256 of the file content (the same for copies of one file) |
| `file_name` | file name without the folder |
| `page` | 1-based PDF page, `null` for formats without pages |
| `chapter` | EPUB item name, or the section number of a MOBI/AZW book, `null` for PDFs and plain text |
| `ocr` | `true` when the text came from OCR of a scanned page |
| `ocr_confidence` | mean Tesseract word confidence (0–100) of that page, `null` when not measured |

The OCR cache stores the confidence beside the page text. Cache files written before this change still load; their pages have `ocr_confidence: null` until the cache entry is removed and the page is OCR'd again.

Points indexed before this change have none of the new fields. Citations then fall back to `source_path` and `locator`. A full re-index (`POST /ingest/reindex` with `full_reindex: true`) fills the fields in.

### How the answer is built

1. Retrieved chunks are numbered `[1]…[n]` in the prompt, with the title, file name and page or chapter. Only whole chunks that fit in `MAX_CONTEXT_CHARS` are included, and only those become citations.
2. Uncertain chunks are marked `BIZONYTALAN: …` in the prompt. A chunk is uncertain when its retrieval score is below `RETRIEVAL_WEAK_SCORE` (`weak_match`), when its OCR confidence is below `OCR_LOW_CONFIDENCE` (`ocr_low_confidence`), or when it is OCR text with no measured confidence (`ocr_unmeasured`).
3. The prompt (`teacher_common.citations.CITATION_RULES`, added to every task so a custom `TEACHER_PROMPT_PATH` keeps it) tells the model to put `[n]` after each statement taken from source n, to mark its own statements with `[forrás nélkül]`, and to say when a source it used is uncertain.
4. `citations.assess` reads the markers back from the answer. Numbers the model invented are ignored.

### Response

```json
{
  "answer": "A magyar ragozó nyelv [1]. Ez könnyen tanulható [forrás nélkül].",
  "citations": [
    {"id": 1, "file_id": "…", "file_name": "nyelvtan.pdf", "source_path": "/library/nyelvtan.pdf", "title": "Nyelvtan",
     "page": 12, "chapter": null, "locator": "oldal 12 (OCR)", "label": "nyelvtan.pdf, 12. oldal", "score": 0.84,
     "ocr": true, "ocr_confidence": 52.3, "uncertain": true, "uncertain_reasons": ["ocr_low_confidence"],
     "cited": true, "chunk_id": "…", "preview": "…"}
  ],
  "grounding": {"status": "partial", "cited": [1], "unsourced_statements": true, "low_confidence": true,
                "notice": "A [forrás nélkül] jelölésű részek … ellenőrizd az eredeti dokumentumban."},
  "sources": ["… the raw retrieval hits, kept for older clients …"]
}
```

`grounding.status`:

- `grounded`: the answer cites at least one source and marks nothing as its own.
- `partial`: it cites sources and also has `[forrás nélkül]` statements.
- `no_source`: nothing was retrieved (the model is not called), or the answer cites none of the sources.

`grounding.low_confidence` is true when a cited source is uncertain, or, for an answer that cites nothing, when every retrieved source is uncertain. `notice` is a Hungarian sentence for the user, or `null` when the answer is fully grounded on certain sources. The web UI shows it above the answer, highlights the `[n]` markers (hovering one shows the file and page), and lists the sources with OCR, uncertainty and "not cited" badges.

The two thresholds depend on the embedding model and the scans, so treat the defaults as starting points to tune on your own library. `multilingual-e5` puts most scores between 0.7 and 1.0, so 0.8 marks only the lower end as weak. A Tesseract confidence below 70 usually means a page with visible recognition errors.

Stored quizzes and flashcards keep `file_id`, `file_name`, `page` and `chapter` in their source references as well.

### Consistency on re-index and delete

- Re-indexing a file deletes all of its points (by `source_path`) before writing the new ones, and replaces its chunk rows in SQLite. Deleting a file, or unwatching its folder, deletes its points.
- Chunk and point ids include a short digest of the path, so two copies of one file each keep their own points. Before, the copies had the same ids and the second overwrote the first.
- If a file changed and its new content fails to index, its old points and chunk rows are dropped, so answers never cite text the file no longer has. The record stays `failed`, and the next run tries again. A file that fails again without changing (for example a failed PDF being retried) keeps its chunks.
- Retrieval filters are unchanged: `search()` still takes the same `source_paths` filter, and the new fields are only read from the payload.

## Why Qdrant + SQLite

- Qdrant is simple, maintained, and purpose-built for vectors.
- SQLite keeps metadata local without another service.
- This is smaller and easier to migrate than a full Postgres stack for the MVP.

## Stored Study Material

Quizzes and flashcards reuse the same retrieval and Ollama path but ask for JSON (`format: json`) with numbered sources, validate it, retry on malformed output, and persist the result. See "Stored Quizzes and Flashcards" in [architecture](architecture.md).
