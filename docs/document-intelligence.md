# Document Intelligence

Whole-document jobs that go beyond chat: a summary of a whole document, structured extraction, comparison and gap
analysis, a task list with solution proposals, and Hungarian-English translation. Everything runs on the local model,
in the background, per person and workspace, and every result item points back to its source file and page or
chapter. The panel is "Dokumentumelemzés" on the main page; the routes are under `/docintel`.

## What each job does

| Job | Result |
| --- | --- |
| `summary` | Points with sources for the whole document, optionally per chapter (EPUB/MOBI chapters; for PDFs ranges of 10 pages; otherwise runs of chunks) plus an overall summary. Several documents are summarised each on its own. |
| `extract` | One table of concepts, requirements, deadlines, obligations and tables. Each row: type, text, detail, source file and page or chapter, uncertainty flag. Export as CSV or Markdown. |
| `compare` | `diff` (what differs between two documents or versions) or `gap` (what the first, the reference, requires that the second lacks, or repeats only in part, such as with another number). Each finding is `evidenced` or `uncertain`, with the statements and sources from both sides. |
| `tasks` | Tasks, obligations, deadlines and requirements found in the document (source data, with sources), and, in a separate block, solution proposals. A proposal is the model's own conclusion, marked as such. |
| `translate` | The whole document translated hu-en or en-hu, as segments that each name the pages they came from. Download as text or Markdown. |

`/summary` (chat) still works from the few retrieved chunks; a summary job reads every chunk of the document.

## How a long document is read

Ingestion keeps the chunk text only in the vector store (SQLite holds a short preview), each chunk with its position,
page or chapter and OCR confidence. A job rebuilds the document from there in reading order
(`teacher_common/doc_text.py`: `load_chunks`, `trim_overlap`, `make_windows`; the chunker's overlap is trimmed so
sentences are not doubled). The chunks are cut into windows that fit the context: `DOCINTEL_WINDOW_CHARS`, by default
half of `MAX_CONTEXT_CHARS`, and a third of that for translation, whose answer is as long as its input. A chunk that is
larger than the window (the setting was lowered after indexing) is cut into pieces at sentence or word boundaries, each
keeping the page or chapter; the budget holds, and nothing is dropped.

Map-reduce:

1. **Map**: each window is one model call. Its passages are numbered 1..k and the model answers in JSON, citing those
   numbers.
2. **Reduce** (summary, tasks): the points so far are numbered and merged by further calls, level by level, until they
   fit one call. A merged point cites the numbered points it is made from and inherits their sources.
   Extraction needs no model reduce: rows are merged by equal text, keeping all sources (a table is merged only with an
   identical table, so two tables under one caption stay two). A table whose rows are not as wide as its header is
   unusable output and is asked for again.
3. **Compare**: both documents are extracted the same way, then statements are paired deterministically by word-stem
   overlap. Near-identical statements are never reported. Only differing pairs and statements without a counterpart go to
   the model, which says whether they really differ or only say the same in other words.

## Grounding

- Every item cites the numbered passages it stands on. A number outside the window is an invented number and is dropped
  (the same reader as the chat's, `citations.cited_numbers`). An item left with no valid source is rejected and counted
  in the result's notice; it is never shown as sourced.
- The cited chunks get the result's citation numbers 1..N in reading order, with file, page or chapter, OCR flag and
  uncertainty, as in chat. `citations.assess` rates the whole result (`grounding`), so weak or OCR-doubtful sources are
  flagged the same way.
- An absence ("missing from the other document") is `evidenced` only when the other document's whole text holds nothing
  close to it (a lexical check, not the model's word); if something close exists it is `uncertain`, and that passage is
  shown. A change is `evidenced` when both statements exist with sources. A finding the model did not judge is
  `uncertain`. The thresholds are heuristics.
- Output that is not valid JSON is asked for again once, naming the problem. A window that still fails is given up on
  and listed in the result ("these parts could not be processed"); if more than half fail, the job fails with a clear
  message instead of showing a mostly empty result. A translation with a hole is not skipped: the job fails and can be
  resumed.

## Jobs run in the background

- A job is a row in SQLite with status (`queued`, `running`, `succeeded`, `failed`, `cancelled`, `interrupted`),
  progress (`done`/`total` steps and the phase), error text and timestamps. The UI polls it.
- One worker thread in the API process runs **one job at a time**, so one model call at a time. **Chat goes first**: a
  job's model call starts only while no chat or study request is in flight or waiting, and a chat request that arrives
  in the middle of a job's call waits for that one call to end (a sent call cannot be taken back) and then goes first.
  So a long window delays chat by up to one call: lower `DOCINTEL_WINDOW_CHARS` if chat must stay quick on a slow model.
- **Cancel** takes effect after the model call in progress, also when that was the last one: a job cancelled during its
  final call ends cancelled, without a result. A queued job is cancelled at once.
- Each finished step (window, merge, batch) is stored. After a restart, jobs that were running become `interrupted`
  (never stuck as running); `Folytatás` re-queues an interrupted or failed job and only the missing steps run again. A
  resumed job refuses a document that changed or is no longer accessible. Nothing resumes by itself: after a restart the
  person decides.
- Results are stored apart from the job (`docintel_results`), survive restarts and are never written to the vector store
  or the document list.
- Limits: `DOCINTEL_MAX_QUEUED_JOBS` waiting jobs per person, `DOCINTEL_LLM_TIMEOUT_SECONDS` per model call,
  `DOCINTEL_MAX_ATTEMPTS` tries per call.

## Privacy and isolation

- A job and its result belong to the person who started them, in their own workspace. Every route filters by that person
  and by the workspaces the server derives from the session, so sharing a personal workspace shares its documents, not
  its jobs or results; another person's job, result or export answers 404, and a job cannot be started on a document the
  person cannot read. A document shared by read access can be used, and the result stays with the person who made it.
  If the share is withdrawn, a resumed job refuses the document; results already made are kept, as with study items.
- All processing is on the local model; the private services have no egress and this feature adds none. Uploaded and
  processed material is not used for training or fine-tuning: nothing is ever sent anywhere and no weights are changed.
- **Deleting a source document** keeps the results made from it but marks them **stale** ("the source document was
  deleted"), also when a re-index changed the document; unfinished jobs on it are cancelled (a running one stops before
  its next step). This holds whether the document is deleted in the app or removed by an indexing scan. This differs from quizzes and
  decks, which are deleted with their source, because a result here is something the person asked for and may still want
  to export. It stays until they delete it (`DELETE /docintel/jobs/{id}`). A translation holds the document's whole text,
  so delete it too if the source must be gone.
- Text in a document is data. The prompts say so, and the output is parsed and validated, but a hostile document may still
  steer what the model writes; the source check applies to the sources, not to the wording.
- CSV export prefixes a cell that starts with `=`, `+`, `-` or `@`, so a spreadsheet does not run it as a formula.

## Quality

Extraction, comparison and above all translation depend on the model. A small model on a CPU, as in the end-to-end
trial, gives poor results, and nothing here is tuned for it: the tests and the trial check structure, citations and
storage, not wording. Use the largest model the host can run, expect hours for a book, and check what matters in the
source through the page references. Scanned pages carry OCR errors; their sources are flagged uncertain.

## API

All routes need the login session, and POST/DELETE the CSRF token.

| Route | Purpose |
| --- | --- |
| `POST /docintel/jobs` | `{kind, source_paths, granularity?, types?, mode?, direction?}` → 202 and the job. `compare` takes two paths. |
| `GET /docintel/jobs` | The person's jobs (`?kind=`). |
| `GET /docintel/jobs/{id}` | Status, progress, error, `resumable`, `stale`. |
| `GET /docintel/jobs/{id}/result` | `{job, result}`; 409 until there is a result. |
| `GET /docintel/jobs/{id}/export?format=csv\|md\|txt` | Download (`txt` for translations only; `csv` for everything else). |
| `POST /docintel/jobs/{id}/cancel`, `/resume` | Stop a queued or running job; re-queue an interrupted or failed one. |
| `DELETE /docintel/jobs/{id}` | Remove the job and its result (not while it runs). |

Prompts are in `config/prompts/docintel_*_hu.md`, in Hungarian like the others.

## Not built yet

- Adding a translation (or any result) to the search index on the person's choice. Today results are never indexed.
- Pages or chapters of a source in a result are plain labels; there is no link to open the original at that page.
- Tables are extracted from the text layer the way the parser flattened it; complex PDF tables come out poorly.
