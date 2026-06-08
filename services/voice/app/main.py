import logging
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

from stt import transcribe

logger = logging.getLogger("uvicorn")

WHISPER_MODEL = "base"
WHISPER_DEVICE = "auto"


class SpeakRequest(BaseModel):
    text: str


app = FastAPI(title="teacher-agent-voice", version="0.1.0")


@app.on_event("startup")
def startup_event():
    import os
    global WHISPER_MODEL, WHISPER_DEVICE
    WHISPER_MODEL = os.getenv("VOICE_STT_MODEL", "base")
    WHISPER_DEVICE = os.getenv("VOICE_STT_DEVICE", "auto")
    logger.info("Voice STT model=%s device=%s", WHISPER_MODEL, WHISPER_DEVICE)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "stt_model": WHISPER_MODEL,
        "stt_device": WHISPER_DEVICE,
    }


@app.post("/voice/transcribe")
async def transcribe_audio(audio: UploadFile = File(...)):
    suffix = Path(audio.filename or "audio.wav").suffix or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        content = await audio.read()
        tmp.write(content)
        tmp_path = tmp.name
    try:
        text = transcribe(Path(tmp_path), WHISPER_MODEL, WHISPER_DEVICE)
        return {"text": text, "model": WHISPER_MODEL}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@app.post("/voice/speak")
def speak(payload: SpeakRequest):
    raise HTTPException(
        status_code=501,
        detail="TTS synthesis is not enabled yet.",
    )
