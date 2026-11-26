# Acceptance checklist

Each acceptance criterion, the test, script or manual step that proves it, and where that proof stands today. Run commands from the repository root.

Status values:

- **Automated**: a test or script proves it without a person; `make acceptance` runs the fast part.
- **Manual**: a person follows the steps on a running stack with representative data.
- **Target hardware**: cannot be proven until the stack runs on the deployment host (GPUs, real network, real library).
- **In progress**: the feature is being built; the row is a placeholder to fill when it lands.

A criterion is accepted when every one of its rows passes.

| # | Criterion | Proof | Status |
|---|---|---|---|
| a1 | Offline operation: assistant answers with no internet | `make test-e2e` runs the whole stack in an isolated copy, on the `internal` private network, with a small local model and Hungarian fixtures | Automated (needs Docker) |
| a2 | HU↔EN text processing | `pytest tests/test_docintel.py` (extract, compare, translate with a fake model); `make eval-models` scores the real chat model on Hungarian teaching tasks | Automated + manual (eval read by a person) |
| a3 | Q&A over own documents | `pytest tests/test_e2e_flow.py`; `make test-e2e` (ingest a Hungarian PDF, ask, get a grounded answer) | Automated |
| a4 | All of (a) on the deployment host | Provision models, cut the WAN uplink, ask a HU→EN and an EN→HU translation and a library question; all answer | Target hardware |
| b1 | Answers name the source file and page or chapter | `pytest tests/test_citations.py` (page, chapter and OCR page carried from chunk to answer) | Automated |
| b2 | Uncertain sources are flagged | `pytest tests/test_citations.py tests/test_ocr.py` (weak matches and low OCR confidence are marked uncertain) | Automated |
| b3 | Citations are right on real material | Ask about a known passage in a scanned PDF and an EPUB from the library; open the cited page or chapter | Manual |
| c1 | No leak of private documents or indexes between users | `pytest tests/test_document_isolation.py tests/test_qdrant_workspace_filter.py tests/test_api_auth.py` | Automated |
| c2 | Same, on a running stack | Two accounts upload a private document each; each asks about the other's; neither sees it in answers, lists or downloads | Manual |
| d1 | Photo OCR and translation | *To fill: photo task's tests (for example `tests/test_media.py`)* | In progress |
| d2 | Error marking on unsure OCR or translation | *To fill: test or manual step with a degraded photo* | In progress |
| d3 | Optional vocabulary save | *To fill: test that saving is opt-in and lands in the user's own vocabulary* | In progress |
| d4 | Photo from a phone over the deployed HTTPS URL | Take a photo of a printed page on a phone; text, translation and marks appear | Target hardware |
| e1 | Voice tutor conversation | *To fill: tutor task's tests and a spoken HU/EN practice session* | In progress |
| e2 | 30/60/90 s elevator speech | *To fill: smart talk task's tests; record each length, check timing and speech metrics* | In progress |
| f1 | Interpreter latency measured | *To fill: benchmark command, p50/p95 per direction, Fast and Pro modes* | In progress |
| f2 | Interpreter accuracy measured | *To fill: HU↔EN reference corpus and score* | In progress |
| f3 | Interpreter concurrency measured | *To fill: load run with N simultaneous sessions* | In progress |
| f4 | Same numbers on the deployment GPUs | Repeat f1–f3 on the target host with the chosen GPU placement (see [model-selection.md](model-selection.md)) | Target hardware |
| g1 | Audiobook start and resume | `pytest tests/test_listening.py tests/test_listen_api.py` | Automated |
| g2 | Audiobook on a phone | Start a library document, stop, reopen later; it resumes where it stopped | Manual |
| g3 | Meeting transcripts with timestamps | *To fill: meeting task's tests; an audio file with known speech at known times* | In progress |
| h1 | Outbound traffic to external AI blocked: config | `make test-egress` without a stack (`--config-only`) audits the merged Compose config: private services only on the `internal` network, no published ports | Automated |
| h2 | Same, proven on running containers | `make test-egress` with the stack up: inspects each private container and probes the internet from inside the private network (must fail) | Automated (needs Docker) |
| h3 | Same, on the deployment host and its firewall | Run `make test-egress` there; from a private container, try a public AI API host; it must not connect | Target hardware |
| i1 | Restore a document, a user setting and an index from backup | `make test-backup-restore` seeds state, backs up, changes and deletes it, restores, then checks the document, the saved setting and a searchable index point | Automated (needs Docker) |
| i2 | Rollback | `pytest tests/test_backup_restore.py` (undo point kept, failed restore rolls back); then on a running stack: `make backup`, make a change, `make restore CONFIRM=YES` from the earlier backup, check the change is gone | Automated + manual |
| i3 | Restore on the deployment host and its NAS share | Same as i2 with the real data folder and backup target | Target hardware |

## Automated subset

`make acceptance` runs the unit and integration tests above (no Docker needed) and the Compose egress config audit. The Docker-backed proofs run separately:

```sh
make acceptance            # fast subset
make test-e2e              # a1, a3: isolated stack end to end
make test-egress           # h1, h2: with the stack running
make test-backup-restore   # i1
```

When a feature in progress lands, put its tests in its rows, add them to `ACCEPTANCE_TESTS` in the `Makefile`, and change the status.
