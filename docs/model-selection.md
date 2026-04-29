# Model Selection

The limiting factor is `1x RTX 3070` with `8 GB VRAM`, plus only `16 GB system RAM`.

## Practical Default

- Runtime: `Ollama`
- Starting chat model class: quantized `7B` to `8B` instruct
- Starting embedding model: `intfloat/multilingual-e5-base` on CPU

## Recommended Starting Point

- `qwen2.5:7b-instruct-q4_K_M` as the first candidate

Reasoning:

- broadly capable instruct family
- good multilingual behavior
- realistic fit target for the available GPU class when quantized

## Safe Alternatives

- smaller CPU fallback such as `gemma2:2b`
- another quantized multilingual instruct model if Hungarian quality is better in practice

## What Not To Assume

- Do not assume a 13B+ model is comfortable on `8 GB VRAM`.
- Do not assume a model that looks good in benchmarks will teach well in Hungarian without testing.
- Do not assume GPU inference is mandatory for dev mode.

## NVIDIA Notes

- Install a working host driver first.
- Install NVIDIA Container Toolkit for Docker GPU passthrough.
- Verify host GPU with `nvidia-smi`.
- Verify container GPU visibility with a CUDA container or an Ollama GPU-enabled run.

