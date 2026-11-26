# Model Selection

The limiting factors are the VRAM of a single NVIDIA GPU and the system RAM of the host. The chat model must fit into one GPU's VRAM unless you configure layer-split across several GPUs. Budget VRAM as model weights plus context (KV cache) plus any other concurrent GPU workload (for example Whisper on the GPU), and leave headroom. The guidance below is generic; scale it to the VRAM and RAM you have.

## Per-service GPU placement

On a CPU-only or single-GPU host nothing here needs changing: without the NVIDIA overlays these settings are ignored, and with them every service defaults to card `0`, as before.

On a host with several NVIDIA GPUs, memory is per card, not pooled, so place each GPU service on a card of its own where it can. The NVIDIA overlays read one host GPU index per service from `.env` (the index `nvidia-smi` shows; ansible sets the same variables from `teacher_*_gpu_device_id` in `group_vars`):

| Variable | Service | GPU work | Overlay |
| --- | --- | --- | --- |
| `OLLAMA_GPU_DEVICE_ID` | `ollama` | chat LLM; also the optional vision model for photo OCR | `docker-compose.nvidia.example.yml` |
| `API_GPU_DEVICE_ID` | `api` | query embeddings, when `EMBEDDING_DEVICE=cuda` | `docker-compose.nvidia.example.yml` |
| `INGESTION_GPU_DEVICE_ID` | `ingestion` | indexing embeddings, when `EMBEDDING_DEVICE=cuda` | `docker-compose.nvidia.example.yml` |
| `VOICE_GPU_DEVICE_ID` | `voice` | Whisper STT | `docker-compose.voice.nvidia.example.yml` |
| `OLLAMA_SECOND_GPU_DEVICE_ID` | `ollama-second` | a second model, e.g. a fast translation model | `docker-compose.ollama-second.nvidia.example.yml` |

Piper TTS and Tesseract OCR run on the CPU and need no card. Each container sees only its assigned card, which it calls `cuda:0`; so `EMBEDDING_DEVICE=cuda` (or `cuda:0`) is right whichever host card is chosen. The API and ingestion embed with the same model, so put them on the same card or budget the embedding model twice.

Example for a host with several cards (illustrative):

```sh
OLLAMA_GPU_DEVICE_ID=0          # chat model
OLLAMA_SECOND_GPU_DEVICE_ID=1   # fast translation model
VOICE_GPU_DEVICE_ID=2           # Whisper
API_GPU_DEVICE_ID=3             # embeddings
INGESTION_GPU_DEVICE_ID=3
```

```sh
docker compose -f compose/docker-compose.yml \
  -f compose/docker-compose.nvidia.example.yml \
  -f compose/docker-compose.ollama-second.nvidia.example.yml \
  -f compose/docker-compose.voice.example.yml \
  -f compose/docker-compose.voice.nvidia.example.yml config --quiet
```

### Several Ollama instances

One Ollama instance with several cards visible decides by itself where each model loads, and may unload one model to make room for another. For predictable placement, run one instance per card and model instead. `compose/docker-compose.ollama-second.nvidia.example.yml` adds `ollama-second`: its own card, its own model folder (`${TEACHER_DATA_DIR}/ollama-second`), the same private network and offline settings as `ollama`, and it is covered by `make test-egress`. Pull its model into it the same way as for `ollama`. The app currently reads one `OLLAMA_BASE_URL`: set it to `http://ollama-second:11434` to move all LLM calls there, or give a feature (such as translation) its own URL setting to use both. Copy the overlay, renaming the service, its variable and its model folder, for a third instance.

### A model too big for one card

Keep a model on one card whenever its weights plus context fit there: that is the fastest and least fragile setup. When it does not fit:

- **Ollama** splits a model's layers across all the cards it can see when it does not fit one. Give the `ollama` service several cards in a site-specific override, e.g. `device_ids: ["0", "1"]`, rather than a single variable. Leave `OLLAMA_SCHED_SPREAD` unset so models that fit one card still stay on one.
- **llama.cpp** (`llama-server`) does the same with `--split-mode layer` and `--tensor-split 1,1` (proportions per card), with `-ngl` for the number of layers on the GPUs.
- Prefer **layer** split: each card holds whole layers and only the activations between them cross PCIe once per token. Avoid **row** (tensor) split on narrow PCIe links: it exchanges data inside every layer and is usually slower than one card.
- Each card still needs room for its share of the layers plus its share of the context (KV cache); the split does not pool memory. Expect slower token generation than on one card, and a longer first load over narrow links.
- Do not let the remainder spill to the CPU on a host with a weak CPU and little RAM: it is much slower and uses scarce system memory. Prefer a smaller model or a stronger quantization that fits one card or a split across two.

### VRAM budget per service

Guidance only: actual use depends on model, quantization, context length, batch size, concurrency and backend version. Measure with `nvidia-smi` under load and keep about 10–15% of each card free.

| Service | What it holds in VRAM | Guidance |
| --- | --- | --- |
| Chat LLM (`ollama`) | weights + KV cache for the context + runtime overhead | The largest consumer; give it a card of its own. For example, a 7B–8B model at 4-bit quantization with a modest context fits an 8 GB card; a longer context or a bigger model needs a split or a bigger card. |
| Second model (`ollama-second`) | as above | A small fast model (for example 1B–4B at 4-bit) leaves room on an 8 GB card for a longer context or another small service. |
| Vision OCR (Ollama vision model, optional) | weights + image tokens | Loaded in the instance that serves it; budget it like a chat model of its size, or it will evict the chat model. |
| STT (`voice`, Whisper) | weights + decoding buffers | Small to moderate (roughly 1–2 GB for a small or medium quantized model, more for large ones). Can share a card with embeddings; unloads when idle. |
| Embeddings (`api`, `ingestion`) | weights + batch activations | Usually well under 2 GB for a sentence-transformer model; batches during indexing add to it. Can share a card with STT. |
| TTS (Piper) | none | Runs on the CPU. |
| OCR (Tesseract) | none | Runs on the CPU. |

## Practical Default

- Runtime: `Ollama`
- Starting chat model class: quantized `7B` to `8B` instruct
- Starting embedding model: `intfloat/multilingual-e5-base` on CPU

## Recommended Starting Point

This is a first guess, not a result. Pick the model with the evaluation below.

- `qwen2.5:7b-instruct-q4_K_M` as the first candidate

Reasoning:

- broadly capable instruct family
- good multilingual behavior
- a realistic fit for small-to-mid VRAM budgets when quantized

## Safe Alternatives

- smaller CPU fallback such as `gemma2:2b`
- another quantized multilingual instruct model if Hungarian quality is better in practice

## What Not To Assume

- Do not assume a 13B+ model is comfortable on a small-VRAM GPU; check the model's VRAM need (weights plus context, plus other GPU workloads) against the VRAM available to it.
- Do not assume a model that looks good in benchmarks will teach well in Hungarian without testing.
- Do not assume GPU inference is mandatory for dev mode.

## Changing the Embedding Model

Back up before changing `EMBEDDING_MODEL` or rebuilding the api/ingestion images. The current collection stores vectors without the embedding model identity. Startup creates the collection only when it is absent; it does not compare the stored vector size or model with the configured model. A dimension change is rejected by Qdrant at search/upsert time. A same-dimension change can leave old vectors alongside new vectors. The current full reindex does not recreate the Qdrant collection schema, so it does not make a dimension change safe.

Treat embedding-model migration as unsupported until there is an explicit collection migration. Keep the previous api/ingestion images and model setting, plus a pre-change backup. To revert, stop api and ingestion, restore that backup with `make restore CONFIRM=YES RESTORE_FROM=<backup>`, restore the old setting and image digests, then start the previous stack.

## NVIDIA Notes

- Install a working host driver first.
- Install NVIDIA Container Toolkit for Docker GPU passthrough.
- Verify host GPU with `nvidia-smi`.
- Verify container GPU visibility with a CUDA container or an Ollama GPU-enabled run.


## Choosing the Chat Model: Candidates and Evaluation

`config/models/eval_candidates.example.json` lists a shortlist with the Ollama tag and quantisation, licence, a reason, and whether each should fit one GPU or needs a layer split. Its tags and sizes were read from the Ollama library pages; the language-quality claims are only partly backed: the published HuGME results (hugme.nytud.hu/Results/) and OpenHuEval cover some of these models on language proficiency and knowledge, not on grounded teaching answers, so the run, not the list, decides. It includes two Hungarian-focused models (the research model Racka-4B, licensed non-commercially, and a community Gemma 3 fine-tune) and notes the ones left out and why. Check each licence before you rely on a model; two entries use Gemma terms, which restrict some uses.

`scripts/eval_models.py` runs every candidate through the stack's own Ollama with the real teacher prompt (`config/prompts/teacher_hu.md`, citation rules, quiz and flashcard templates), the same way the API builds its prompts, and writes a results file and a report.

### Run it

1. Start the stack (`make dev-up`) and fetch the candidates through the one deliberate channel: `scripts/model-update.sh <tag> [<tag> ...]`. The script never downloads anything itself; it lists what is missing and prints this command.
2. `make eval-models` (or `python3 scripts/eval_models.py run --candidates <file>`). It needs Python 3 and `pydantic` on the host (the same parser the study feature uses validates the quiz JSON), and `nvidia-smi` if you want per-GPU memory.
3. Requests go through `docker compose exec api`, so they use the private network like the app does: no port is published and no private service gets egress. `--url http://localhost:11434` talks to a local Ollama directly instead; only loopback addresses are accepted, because the prompts contain your documents (`--allow-remote-url` overrides that deliberately).
4. Useful options: `--only <tag part>`, `--only-questions <id part>`, `--num-ctx`, `--max-context-chars` (default 18000, the app's `MAX_CONTEXT_CHARS`; keep it equal to your `.env`), `--no-blind` (show model names), `--candidates <your copy>`.

Copy the candidate file and edit it for your own list; set `EVAL_CANDIDATES` and `EVAL_ARGS` for `make eval-models`.

### Your own questions and passages

The built-in set (`config/eval/questions_hu.json`) is small and generic: an explanation, question answering from a passage with a citation, a question the passage cannot answer, a quiz and a flashcard deck in JSON, a summary, and an OCR-noisy passage. For a real decision add your own: `--private /path/to/questions.private.json` (repeatable) with the same shape (see the built-in file: `id`, `task`, `question`, `sources` with `title`, `locator`, `text`, optional `ocr`/`ocr_confidence`, and `expect`). Keep that file outside the repository, or name it `*.private.json` or put it in `eval-private/`, which git ignores. Results go to `eval-results/<time>/`, also ignored; the script refuses to write inside the repository to a place git does not ignore. Your documents and the model's answers about them must not be committed.

### What it measures

Per model: time to first token (the first question includes loading the model, shown separately as load time), tokens per second, total time, where Ollama put the model (one GPU, several GPUs, split between GPU and CPU, or CPU only, from `/api/ps`) and the memory each GPU gained, from `nvidia-smi` sampled during the run. A model that does not fit one GPU is split or partly offloaded to the CPU. How slow that is depends on your host (the links between GPUs, system RAM, CPU), so it is measured here rather than assumed: compare tokens per second and time to first token of the split models with the single-GPU ones. Memory figures are the increase over the idle baseline, so other GPU workloads (for example speech recognition) distort them; run on idle GPUs.

Automatic checks, only where a script can be honest: quiz and flashcard output is valid JSON in the study feature's format, with the requested count; a citation `[n]` that exists is given; the answer, or the text fields of a quiz or deck, is Hungarian (word-list heuristic); the model declines when the passage lacks the answer (and invents no numbers); an OCR-flagged source is called uncertain; an expected fact appears. These catch formatting and obedience failures, not language quality.

### Deciding

Language quality is rated by you. Open `report.md`: answers sit side by side per question, with the model names hidden (Modell A, B, ...). Under each answer fill in `**Értékelés (1-5):** 4`. Then `python3 scripts/eval_models.py totals eval-results/<time>/report.md` prints each model's rating sum and mean next to its checks passed and speed.

A reasonable rule: drop models that fail the JSON or refusal checks often; among the rest, take the best-rated model whose speed you accept on your hardware; prefer one that fits a single GPU unless a split model is clearly better in the ratings and its measured speed is tolerable. Then set `OLLAMA_MODEL` and run `make model-update`. Re-run the evaluation when you change the model or the prompt.
