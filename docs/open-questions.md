# Open Questions

## Open

- What exact document formats are present in the real library? How many Kindle books carry DRM (those are skipped, never decrypted)?
- What share of the library is scanned? OCR handles both, but a large scanned share means a long first pass (see `docs/operations.md`, "Long OCR Runs").
- Is SMB/CIFS or NFS the preferred share protocol?
- What is the approximate library size and document count?
- Is the GPU dedicated to this host permanently, or shared with other workloads (and if shared, how much VRAM is left for the chat model)?
- Which Hungarian-capable chat model performs best on the actual materials?
- Is offline Hungarian TTS quality good enough to justify the extra service? Piper's Hungarian voices are intelligible and need no GPU, so the service is built as an optional overlay (see `docs/voice-hungarian.md`). Whether users like Imre, Anna or Berta is still to be tried on the phone.
- Is CPU dictation fast enough on the host, or should Whisper move to the GPU?
- Should the Ansible deploy use the Compose HTTPS overlay instead of the host Caddy role? (Today local dev uses the overlay and deploys use the role; see `docs/https.md`.)

## Answered

- ~~Is scheduled indexing preferred over manual reindex?~~ Both: scheduled indexing (interval or cron) of watched folders, with manual reindex kept and sharing one lock. See `docs/document-ingestion.md`.
- ~~Should scanned PDFs be indexed?~~ Yes, they are OCR'd: Tesseract (`hun+eng`) for PDF pages without a text layer, cached per page and resumed across runs. Image-only EPUB/DOCX are not OCR'd. How much of the library is scanned is still open (above).
- ~~Should quizzes and flashcards be stored, or generated on demand only?~~ Stored: generated quizzes and decks can be saved with their source references, with quiz attempts and SM-2 card reviews. Generating without saving still works.
- ~~Does the app need its own login, or is the VPN enough?~~ Its own login too: argon2 users from configuration, server-side sessions, CSRF and login rate limits, on top of the VPN and HTTPS. See `docs/security.md`.
- ~~Should MOBI/AZW/KF8 be supported?~~ Yes, DRM-free ones are unpacked in-process (pure-Python `mobi`) and indexed like EPUB/HTML. See `docs/document-ingestion.md`.
- ~~Should HTTPS terminate inside the Compose stack?~~ For local dev and LAN/phone testing, yes: a Caddy overlay (`compose/docker-compose.proxy.yml`, part of `make dev-up`) with an internal CA by default. See `docs/https.md`.
- ~~Index the whole share or chosen folders?~~ Only folders listed under `watch.folders` in `config/ingestion/ingestion.yml`.
