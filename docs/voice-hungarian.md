# Voice Hungarian

Voice is staged separately so the text RAG MVP is not blocked.

## Tier 1

- text input
- text output
- no voice dependency

## Tier 2 Candidates

### STT

- `faster-whisper`
- `whisper.cpp`

Comments:

- Hungarian transcription quality can be acceptable, but model size and latency vary.
- GPU acceleration helps, but CPU fallback is possible with reduced responsiveness.

### TTS

- `Piper` if a suitable Hungarian voice is available

Comments:

- Voice quality depends entirely on available Hungarian models.
- Keep TTS as a separate service to avoid destabilizing the core RAG path.

## Current Repo State

- `services/voice/` is a placeholder service only
- `compose/docker-compose.voice.example.yml` is an example overlay
- `/voice/transcribe` and `/voice/speak` are reserved endpoints, not full implementations

