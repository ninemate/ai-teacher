# Voice Hungarian

Voice is an optional overlay on top of the text RAG stack. Without it nothing changes: the UI simply does not show the voice controls.

With it, the user can:

- dictate a question in Hungarian (speech-to-text), then check and send it
- have the answer read aloud in Hungarian (text-to-speech), choosing among three voices

Everything runs offline in the `voice` container. Models are downloaded when the image is built, and the running service has `HF_HUB_OFFLINE=1`.

## Decision

| | Engine | Model | Runs on | VRAM |
| --- | --- | --- | --- | --- |
| TTS | [Piper](https://github.com/OHF-Voice/piper1-gpl) (`piper-tts` 1.8) | `hu_HU-imre-medium` (default), `hu_HU-anna-medium`, `hu_HU-berta-medium` | CPU | 0 |
| STT | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) 1.2 (CTranslate2) | `large-v3-turbo`, int8 | CPU by default, GPU optional | 0 (about 1-1.5 GB on the GPU overlay) |

### Why Piper for TTS

- **Hungarian voices exist and are free to use.** Piper has three Hungarian voices (Imre, male; Anna and Berta, female). Each is about 63 MB, the training data is CC0, and they were fine-tuned from the English `lessac` voice.
- **No GPU needed.** On the CPU it synthesizes far faster than real time. The 8 GB card stays free for the chat model and Whisper.
- **Hungarian text handling is built in.** Piper phonemizes with espeak-ng, which reads Hungarian numbers, dates, ordinals ("1. lépés" → "első lépés", "XIV. Lajos" → "tizennegyedik"), units and most abbreviations ("pl.", "stb.", "ill.", "ún.", "vö.") correctly. The few it spells out letter by letter are expanded first (see Text Clean-up).
- **Alternatives considered:**
  - Coqui XTTS-v2 supports Hungarian and sounds more natural, but needs about 2-4 GB of VRAM or is slow on the CPU, and its model licence is non-commercial.
  - Meta MMS-TTS (`facebook/mms-tts-hun`) is lower quality and CC-BY-NC.
  - Plain espeak-ng is robotic.
  - If a better offline Hungarian voice appears, it can replace `tts.py` behind the same endpoint.

Piper's engine is GPL-3.0. It runs as a separate service and is not linked into the rest of the code.

### Why faster-whisper `large-v3-turbo` for STT

- Whisper is the strongest openly available Hungarian recogniser. `large-v3-turbo` keeps the large-v3 encoder with a 4-layer decoder: near large-v3 quality at the cost of `medium`.
- CTranslate2 int8 keeps it small: about 1.7 GB of RAM on the CPU, and roughly 1-1.5 GB of VRAM in `int8_float16` on the GPU.
- `whisper.cpp` would be equally viable. faster-whisper was already in the repo, decodes browser audio (webm/opus, mp4/aac) through PyAV without a system ffmpeg, and has a built-in VAD filter.
- `small` is not good enough for Hungarian (see below), and `base` (the old default) is worse still.

### Measurements

Measured on a 16-core desktop CPU without GPU, 2026-10-29. Piper spoke six Hungarian teaching sentences (57 words); Whisper transcribed them back. This "round-trip" word error rate measures TTS intelligibility and STT accuracy together. It is not a substitute for listening, but it ranks the options. Some counted errors are harmless ("II." for "második").

| | Imre | Anna | Berta |
| --- | --- | --- | --- |
| TTS speed (25 s of speech) | 0.38 s | 0.39 s | 0.36 s |
| WER via `small` | 42% | 33% | 47% |
| WER via `medium` | 14% | 17.5% | 17.5% |
| WER via `large-v3-turbo` | **12.3%** | 17.5% | 14% |

- Whisper CPU time is dominated by the encoder, which always processes a 30 s window. A short question takes about 3.7 s with `large-v3-turbo` and about 3 s with `medium` on this machine, regardless of beam size. Expect proportionally more on a VM with fewer cores.
- Browser recordings (webm/opus from Chrome and Firefox, mp4/aac from Safari) gave the same transcription as WAV.
- Peak memory in the voice container: about 1.7 GB for Whisper, plus about 0.2-0.7 GB for Piper depending on how many voices have been used.

Imre is the default because it was the most intelligible. Which voice sounds best to the listener is a matter of taste, so the UI offers all three and remembers the choice per phone.

### VRAM on the shared RTX 3070

The default uses no GPU memory at all: both engines run on the CPU, and the chat model keeps the whole card.

If dictation on the CPU is too slow on the VM, add `compose/docker-compose.voice.nvidia.example.yml`. It:

- builds the image with cuBLAS and cuDNN (`VOICE_CUDA=true`, about 1.5 GB more image)
- reserves GPU 0
- sets `VOICE_STT_DEVICE=auto`: GPU when available, with a fallback to the CPU if loading on the GPU fails, e.g. out of memory
- unloads Whisper after `VOICE_STT_GPU_IDLE_UNLOAD_SECONDS` (default 300) without use, so Ollama gets the memory back

Whisper turbo in `int8_float16` needs roughly 1-1.5 GB. With `qwen2.5:7b-instruct-q4_K_M` at 8k context (about 5.5-6 GB) that fits on 8 GB, but with little headroom. The GPU path has not been measured on the target card yet.

## Enabling It

Local:

```bash
make dev-up-voice   # dev stack + compose/docker-compose.voice.example.yml
```

The first build downloads Whisper `large-v3-turbo` (about 1.6 GB) and the three Piper voices (about 190 MB) into the image.

Ansible: set `teacher_enable_voice: true` (and optionally `teacher_voice_gpu_enabled: true`) in the inventory's group vars.

Check it:

```bash
docker compose --env-file .env -f compose/docker-compose.yml -f compose/docker-compose.voice.example.yml \
  exec voice python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8090/health').read().decode())"
```

## Configuration

`.env` (passed in by `compose/docker-compose.voice.example.yml`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `VOICE_STT_BACKEND` | `faster-whisper` | `disabled` turns dictation off |
| `VOICE_STT_MODEL` | `large-v3-turbo` | Whisper size or Hugging Face CTranslate2 repo; also a build arg (rebuild after changing). `medium` is the lighter fallback. |
| `VOICE_STT_DEVICE` | `cpu` | `cpu`, `cuda` or `auto` |
| `VOICE_STT_COMPUTE_TYPE` | `auto` | `int8` on the CPU, `int8_float16` on the GPU |
| `VOICE_STT_CPU_THREADS` | `0` | CTranslate2 default; lower it if dictation starves the chat model |
| `VOICE_STT_BEAM_SIZE` | `5` | |
| `VOICE_STT_MAX_SECONDS` | `120` | longer recordings are refused (`413`) |
| `VOICE_STT_GPU_IDLE_UNLOAD_SECONDS` | `300` | GPU overlay only |
| `VOICE_TTS_BACKEND` | `piper` | `disabled` turns read-aloud off |
| `VOICE_TTS_VOICES` | `hu_HU-imre-medium,hu_HU-anna-medium,hu_HU-berta-medium` | baked in at build time; the first is the default |
| `VOICE_TTS_LENGTH_SCALE` | `1.0` | above 1 speaks slower, e.g. `1.15` for an older listener |
| `VOICE_TTS_MAX_CHARS` | `6000` | longer text is refused (`413`) |
| `VOICE_MAX_UPLOAD_MB` | `15` | upload limit, enforced by both the API and the voice service |

## API

The voice service listens only on the internal Compose network (`expose`, no published port). The browser talks to the API, which proxies `/voice/*` behind the normal login, CSRF and origin checks (see [security.md](security.md)).

| Method | Path | Body | Answer |
| --- | --- | --- | --- |
| GET | `/voice/status` | | `{available, stt, tts, voices, default_voice, max_seconds, max_chars}`. `available: false` when the overlay is not running. |
| POST | `/voice/transcribe` | multipart `audio` (webm, ogg, mp4/m4a, mp3, wav, flac) | `{text, language: "hu", duration, elapsed, model}` |
| POST | `/voice/speak` | `{text, voice?}` | `audio/mpeg` (64 kbit/s mono MP3; about a tenth of the WAV size) |

Errors keep the voice service's status and Hungarian message:

- `413`: too large or too long
- `422`: unreadable audio, unknown voice, or nothing to read
- `503`: the voice service is unreachable or the engine is disabled

## Web UI

- **Diktálás** under the question field records with the microphone (`MediaRecorder`) and puts the recognised text into the field. The text is not sent automatically: the user can correct it first.
- Browsers only allow the microphone in a secure context (HTTPS, or `localhost`). Over plain HTTP the button is replaced with **Hangfelvétel feltöltése**, which sends a recording made with the phone's recorder app. Live dictation on the phone therefore needs the HTTPS entrypoint (see [mobile-access.md](mobile-access.md)).
- **Felolvasás** under the answer reads it aloud with the chosen voice, in a normal audio player (pause, seek). The voice choice is kept in the browser's `localStorage`.
- The CSP allows `media-src 'self' blob:` for the generated audio.

## Text Clean-up

Answers come from the chat model and often contain light Markdown. `services/voice/app/speech_text.py` prepares them before Piper sees them:

- Markdown is stripped: headings, bullets, emphasis, code fences, links (keeping the link text), bare URLs, and table rules.
- Every line gets end punctuation, so headings and list items get a pause instead of running together.
- A few abbreviations espeak-ng spells letter by letter are expanded: `kb.` → "körülbelül", `db` → "darab", `i. e.` → "időszámításunk előtt", `i. sz.` → "időszámításunk szerint". Others such as `pl.` and `stb.` are already read correctly and are left alone.

## Code and Tests

- Code: `services/voice/app/`:
  - `main.py`: endpoints
  - `stt.py`: Whisper, lazy load, CPU fallback, idle unload
  - `tts.py`: Piper and MP3 encoding
  - `speech_text.py`
  - `voice_settings.py`
- `tests/test_voice_service.py` and `tests/test_api_voice.py` replace the engines and the voice service with fakes, so CI needs neither the models nor the packages. `tests/test_speech_text.py` covers the clean-up.
- A real round trip (Piper speaks, Whisper hears) runs where the engines are installed:

```bash
pip install -r services/voice/requirements.txt
python -m piper.download_voices --data-dir ./piper-voices hu_HU-imre-medium
VOICE_TEST_PIPER_DIR=./piper-voices python -m pytest tests/test_voice_service.py
```

## Known Limits

- CPU dictation latency depends on the VM's cores. If it is too slow, try `VOICE_STT_MODEL=medium`, or move Whisper to the GPU.
- Piper's `medium` Hungarian voices are clear but noticeably synthetic, and stress on rare or foreign words can be off.
- Long answers are synthesized in one piece before playback starts. On the test machine a 6000-character answer (about 6.5 minutes of speech, 3 MB of MP3) took 8.8 s. Typical answers are well under 2000 characters. Sentence-by-sentence streaming would be the next step if this proves too slow on the VM.
- Hungarian fine-tuned Whisper checkpoints exist on Hugging Face. Any CTranslate2 conversion can be set as `VOICE_STT_MODEL`, but none has been evaluated here.
