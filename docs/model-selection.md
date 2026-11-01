# Model Selection

The limiting factors are the VRAM of a single NVIDIA GPU and the system RAM of the host. The chat model must fit into one GPU's VRAM unless you configure layer-split across several GPUs. Budget VRAM as model weights plus context (KV cache) plus any other concurrent GPU workload (for example Whisper on the GPU), and leave headroom. The guidance below is generic; scale it to the VRAM and RAM you have.

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
4. Useful options: `--only <tag part>`, `--only-questions <id part>`, `--num-ctx`, `--no-blind` (show model names), `--candidates <your copy>`.

Copy the candidate file and edit it for your own list; set `EVAL_CANDIDATES` and `EVAL_ARGS` for `make eval-models`.

### Your own questions and passages

The built-in set (`config/eval/questions_hu.json`) is small and generic: an explanation, question answering from a passage with a citation, a question the passage cannot answer, a quiz and a flashcard deck in JSON, a summary, and an OCR-noisy passage. For a real decision add your own: `--private /path/to/questions.private.json` (repeatable) with the same shape (see the built-in file: `id`, `task`, `question`, `sources` with `title`, `locator`, `text`, optional `ocr`/`ocr_confidence`, and `expect`). Keep that file outside the repository, or name it `*.private.json` or put it in `eval-private/`, which git ignores. Results go to `eval-results/<time>/`, also ignored; the script refuses to write inside the repository to a place git does not ignore. Your documents and the model's answers about them must not be committed.

### What it measures

Per model: time to first token (the first question includes loading the model, shown separately as load time), tokens per second, total time, where Ollama put the model (one GPU, several GPUs, split between GPU and CPU, or CPU only, from `/api/ps`) and the memory each GPU gained, from `nvidia-smi` sampled during the run. A model that does not fit one GPU is split or partly offloaded to the CPU. How slow that is depends on your host (the links between GPUs, system RAM, CPU), so it is measured here rather than assumed: compare tokens per second and time to first token of the split models with the single-GPU ones. Memory figures are the increase over the idle baseline, so other GPU workloads (for example speech recognition) distort them; run on idle GPUs.

Automatic checks, only where a script can be honest: quiz and flashcard output is valid JSON in the study feature's format, with the requested count; a citation `[n]` that exists is given; the answer, or the text fields of a quiz or deck, is Hungarian (word-list heuristic); the model declines when the passage lacks the answer (and invents no numbers); an OCR-flagged source is called uncertain; an expected fact appears. These catch formatting and obedience failures, not language quality.

### Deciding

Language quality is rated by you. Open `report.md`: answers sit side by side per question, with the model names hidden (Modell A, B, ...). Under each answer fill in `**Értékelés (1-5):** 4`. Then `python3 scripts/eval_models.py totals eval-results/<time>/report.md` prints each model's rating sum and mean next to its checks passed and speed.

A reasonable rule: drop models that fail the JSON or refusal checks often; among the rest, take the best-rated model whose speed you accept on your hardware; prefer one that fits a single GPU unless a split model is clearly better in the ratings and its measured speed is tolerable. Then set `OLLAMA_MODEL` and run `make model-update`. Re-run the evaluation when you change the model or the prompt.
