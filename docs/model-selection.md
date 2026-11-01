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

## NVIDIA Notes

- Install a working host driver first.
- Install NVIDIA Container Toolkit for Docker GPU passthrough.
- Verify host GPU with `nvidia-smi`.
- Verify container GPU visibility with a CUDA container or an Ollama GPU-enabled run.

