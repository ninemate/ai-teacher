# Open Questions

## Open

- What exact document formats are present in the real library? (MOBI/AZW/KF8 are reported as failed until conversion lands.)
- What share of the library is scanned? OCR handles both, but a large scanned share means a long first pass (see `docs/operations.md`, "Long OCR Runs").
- Is SMB/CIFS or NFS the preferred share protocol?
- What is the approximate library size and document count?
- Is the RTX 3070 dedicated to this VM permanently?
- Which Hungarian-capable chat model performs best on the actual materials?
- Is offline Hungarian TTS quality good enough to justify the extra service?
- Should HTTPS terminate inside the Compose stack, or stay with the VPN / optional Caddy role?

## Answered

- ~~Is scheduled indexing preferred over manual reindex?~~ Both: scheduled indexing (interval or cron) of watched folders, with manual reindex kept and sharing one lock. See `docs/document-ingestion.md`.
- ~~Are scanned PDFs common enough to need OCR?~~ Yes: Tesseract OCR (`hun+eng`) for PDF pages without a text layer, cached per page and resumed across runs. Image-only EPUB/DOCX are not OCR'd.
- ~~Should quizzes and flashcards be stored, or generated on demand only?~~ Stored: generated quizzes and decks can be saved with their source references, with quiz attempts and SM-2 card reviews. Generating without saving still works.
- ~~Does the app need its own login, or is the VPN enough?~~ Its own login too: argon2 users from configuration, server-side sessions, CSRF and login rate limits, on top of the VPN and HTTPS. See `docs/security.md`.
- ~~Index the whole share or chosen folders?~~ Only folders listed under `watch.folders` in `config/ingestion/ingestion.yml`.
