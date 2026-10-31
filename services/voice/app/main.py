"""Offline Hungarian voice service: speech-to-text (faster-whisper) and text-to-speech (Piper).

Only reachable on the internal Compose network; the API proxies `/voice/*` behind its login.
"""

import logging
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field
from stt import AudioTooLong, AudioUnreadable, Transcriber
from tts import MEDIA_TYPES, AudioFormat, NothingToSay, Synthesizer, UnknownVoice
from voice_settings import VoiceSettings

logger = logging.getLogger("uvicorn")

settings = VoiceSettings.from_env()
transcriber = Transcriber(settings)
synthesizer = Synthesizer(settings)
app = FastAPI(title="teacher-agent-voice", version="0.2.0")


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1)
    voice: str | None = None
    format: AudioFormat = "mp3"


@app.on_event("startup")
def startup_event() -> None:
    logger.info(
        "Voice STT=%s (%s on %s), TTS=%s (voices: %s)",
        settings.stt_backend,
        settings.stt_model,
        settings.stt_device,
        settings.tts_backend,
        ", ".join(settings.tts_voices),
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "stt": {
            "enabled": settings.stt_enabled,
            "backend": settings.stt_backend,
            "model": settings.stt_model,
            "device": transcriber.device or settings.stt_device,
            "loaded": transcriber.loaded,
            "max_seconds": settings.stt_max_seconds,
        },
        "tts": {
            "enabled": settings.tts_enabled,
            "backend": settings.tts_backend,
            "voices": settings.tts_voices if settings.tts_enabled else [],
            "default_voice": synthesizer.default_voice if settings.tts_enabled else None,
            "max_chars": settings.tts_max_chars,
        },
    }


async def _save_upload(audio: UploadFile, target: Path) -> None:
    size = 0
    with target.open("wb") as out:
        while chunk := await audio.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_upload_bytes:
                raise HTTPException(status_code=413, detail="A hangfelvétel túl nagy.")
            out.write(chunk)


@app.post("/voice/transcribe")
async def transcribe_audio(audio: UploadFile = File(...)):
    if not settings.stt_enabled:
        raise HTTPException(status_code=503, detail="A beszédfelismerés ki van kapcsolva.")
    # The suffix is only a hint for the decoder; never use the client's file name as a path.
    suffix = Path(audio.filename or "").suffix.lower()
    suffix = suffix if suffix in {".wav", ".webm", ".ogg", ".mp3", ".m4a", ".mp4", ".aac", ".flac"} else ".audio"
    with tempfile.TemporaryDirectory(prefix="voice-") as tmp:
        path = Path(tmp) / f"upload{suffix}"
        await _save_upload(audio, path)
        try:
            result = await run_in_threadpool(transcriber.transcribe, path)
        except AudioUnreadable as exc:
            raise HTTPException(status_code=422, detail="Nem sikerült beolvasni a hangfelvételt.") from exc
        except AudioTooLong as exc:
            limit = int(settings.stt_max_seconds)
            raise HTTPException(status_code=413, detail=f"A felvétel legfeljebb {limit} másodperces lehet.") from exc
    logger.info("Transcribed %.1fs of audio in %.1fs on %s", result.duration, result.elapsed, result.device)
    return {
        "text": result.text,
        "language": "hu",
        "duration": round(result.duration, 2),
        "elapsed": round(result.elapsed, 2),
        "model": settings.stt_model,
    }


@app.post("/voice/speak")
def speak(payload: SpeakRequest):
    if not settings.tts_enabled:
        raise HTTPException(status_code=503, detail="A felolvasás ki van kapcsolva.")
    if len(payload.text) > settings.tts_max_chars:
        raise HTTPException(
            status_code=413, detail=f"A felolvasandó szöveg legfeljebb {settings.tts_max_chars} karakter lehet."
        )
    try:
        audio = synthesizer.synthesize(payload.text, payload.voice, payload.format)
    except UnknownVoice as exc:
        raise HTTPException(status_code=422, detail="Ismeretlen hang.") from exc
    except NothingToSay as exc:
        raise HTTPException(status_code=422, detail="Nincs felolvasható szöveg.") from exc
    return Response(audio, media_type=MEDIA_TYPES[payload.format])
