# Model Selection

The limiting factors are the VRAM of a single NVIDIA GPU and the system RAM of the host. The chat model must fit into one GPU's VRAM unless you configure layer-split across several GPUs. Budget VRAM as model weights plus context (KV cache) plus any other concurrent GPU workload (for example Whisper on the GPU), and leave headroom. The guidance below is generic; scale it to the VRAM and RAM you have.

## Practical Default

- Runtime: `Ollama`
- Starting chat model class: quantized `7B` to `8B` instruct
- Starting embedding model: `intfloat/multilingual-e5-base` on CPU

## Recommended Starting Point

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

