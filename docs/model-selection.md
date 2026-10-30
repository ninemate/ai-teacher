# Model Selection

The limiting factors are the VRAM of a single NVIDIA GPU and the system RAM of the host. The chat model must fit into one GPU's VRAM unless you configure layer-split across several GPUs. The guidance below assumes a GPU with roughly 8 GB of VRAM and modest system RAM; adjust it upwards if you have more.

## Practical Default

- Runtime: `Ollama`
- Starting chat model class: quantized `7B` to `8B` instruct
- Starting embedding model: `intfloat/multilingual-e5-base` on CPU

## Recommended Starting Point

- `qwen2.5:7b-instruct-q4_K_M` as the first candidate

Reasoning:

- broadly capable instruct family
- good multilingual behavior
- a realistic fit for an 8 GB-class GPU when quantized

## Safe Alternatives

- smaller CPU fallback such as `gemma2:2b`
- another quantized multilingual instruct model if Hungarian quality is better in practice

## What Not To Assume

- Do not assume a 13B+ model is comfortable on an 8 GB-class GPU; check the model's VRAM need (weights plus context) against your card.
- Do not assume a model that looks good in benchmarks will teach well in Hungarian without testing.
- Do not assume GPU inference is mandatory for dev mode.

## NVIDIA Notes

- Install a working host driver first.
- Install NVIDIA Container Toolkit for Docker GPU passthrough.
- Verify host GPU with `nvidia-smi`.
- Verify container GPU visibility with a CUDA container or an Ollama GPU-enabled run.

